import uuid

import pytest
import respx
from sqlalchemy import func, select
from sqlalchemy.orm import Session, sessionmaker

from codemind.core.config import Settings
from codemind.core.errors import CodemindError
from codemind.db.models import (
    DEFAULT_WORKSPACE_ID,
    Chunk,
    File,
    FileContent,
    Job,
    Repository,
    RepositorySnapshot,
    Symbol,
)
from codemind.indexing import pipeline
from codemind.indexing.pipeline import IndexRun
from codemind.repositories.github import GitHubClient
from tests.fixtures import EXPECTED_EXCLUDED, EXPECTED_INCLUDED, SHA, make_tarball

SETTINGS = Settings(max_file_bytes=5000, max_repository_files=100)
TARBALL_URL = f"https://api.github.com/repos/owner/repo/tarball/{SHA}"


def _queued_job(db: Session) -> Job:
    repo = Repository(
        workspace_id=DEFAULT_WORKSPACE_ID, owner="owner", name="repo", default_branch="main"
    )
    db.add(repo)
    db.flush()
    snapshot = RepositorySnapshot(repository_id=repo.id, commit_sha=SHA, requested_ref="main")
    db.add(snapshot)
    db.flush()
    job = Job(workspace_id=DEFAULT_WORKSPACE_ID, snapshot_id=snapshot.id)
    db.add(job)
    db.commit()
    return job


def _run(factory: sessionmaker[Session], job_id: uuid.UUID, final_attempt: bool = True) -> None:
    IndexRun(
        job_id,
        session_factory=factory,
        github_factory=lambda: GitHubClient(SETTINGS),
        settings=SETTINGS,
        final_attempt=final_attempt,
    ).run()


def _counts(db: Session) -> dict[str, int]:
    return {
        m.__tablename__: db.scalar(select(func.count()).select_from(m))
        for m in (File, FileContent, Chunk, Symbol)
    }


@respx.mock
def test_indexes_fixture_repository(session_factory, db: Session) -> None:
    respx.get(TARBALL_URL).respond(content=make_tarball())
    job = _queued_job(db)

    _run(session_factory, job.id)

    db.expire_all()
    job = db.get(Job, job.id)
    snapshot = db.get(RepositorySnapshot, job.snapshot_id)
    assert (job.status, job.progress, job.attempt) == ("succeeded", 100, 1)
    # big.txt is too large and src/broken.py has a syntax error, so the index is partial.
    assert snapshot.status == "partial"
    assert {w["code"] for w in snapshot.warnings} == {"files_too_large", "parse_errors"}
    assert snapshot.indexed_at is not None

    files = {f.path: f for f in db.scalars(select(File).where(File.snapshot_id == snapshot.id))}
    assert {p for p, f in files.items() if f.excluded_reason is None} == EXPECTED_INCLUDED
    assert {
        p: f.excluded_reason for p, f in files.items() if f.excluded_reason
    } == EXPECTED_EXCLUDED
    assert db.get(FileContent, files[".env"].id) is None

    symbols = set(db.scalars(select(Symbol.qualified_name)))
    assert {"greet", "Greeter", "Greeter.greet", "add", "Store.get", "User"} <= symbols
    method = db.scalar(select(Symbol).where(Symbol.qualified_name == "Greeter.greet"))
    parent = db.get(Symbol, method.parent_symbol_id)
    assert parent.qualified_name == "Greeter"

    # Every chunk's text is exactly the cited line range of its file.
    for chunk in db.scalars(select(Chunk)):
        text = db.get(FileContent, chunk.file_id).text
        lines = text.splitlines(keepends=True)
        assert chunk.text == "".join(lines[chunk.start_line - 1 : chunk.end_line])

    # Keyword search works on the generated tsvector column.
    hits = db.scalars(
        select(Chunk.symbol_name).where(
            Chunk.search_tsv.op("@@")(func.plainto_tsquery("simple", "prefix"))
        )
    ).all()
    assert "Greeter" in hits
    assert snapshot.stats["files_included"] == len(EXPECTED_INCLUDED)


@respx.mock
def test_reindexing_does_not_duplicate_rows(session_factory, db: Session) -> None:
    respx.get(TARBALL_URL).respond(content=make_tarball())
    job = _queued_job(db)
    _run(session_factory, job.id)
    first = _counts(db)

    second = Job(workspace_id=DEFAULT_WORKSPACE_ID, snapshot_id=job.snapshot_id)
    db.add(second)
    db.commit()
    _run(session_factory, second.id)

    assert _counts(db) == first
    assert first["files"] == len(EXPECTED_INCLUDED) + len(EXPECTED_EXCLUDED)


@respx.mock
def test_transient_failure_is_retried_then_succeeds(session_factory, db: Session) -> None:
    route = respx.get(TARBALL_URL)
    route.side_effect = [respx.MockResponse(503), respx.MockResponse(200, content=make_tarball())]
    job = _queued_job(db)

    with pytest.raises(CodemindError):
        _run(session_factory, job.id, final_attempt=False)
    db.expire_all()
    assert db.get(Job, job.id).status == "queued"
    assert db.get(Job, job.id).error_code == "fetch_failed"

    _run(session_factory, job.id)
    db.expire_all()
    finished = db.get(Job, job.id)
    assert (finished.status, finished.attempt, finished.error_code) == ("succeeded", 2, None)


@respx.mock
def test_final_failure_marks_job_and_snapshot_failed(session_factory, db: Session) -> None:
    respx.get(TARBALL_URL).respond(503)
    job = _queued_job(db)
    _run(session_factory, job.id, final_attempt=True)
    db.expire_all()
    job = db.get(Job, job.id)
    assert (job.status, job.error_code) == ("failed", "fetch_failed")
    assert db.get(RepositorySnapshot, job.snapshot_id).status == "failed"


@respx.mock
def test_cancel_mid_run(session_factory, db: Session, monkeypatch: pytest.MonkeyPatch) -> None:
    files = {f"src/m{i}.py": f"def f{i}():\n    return {i}\n".encode() for i in range(450)}
    respx.get(TARBALL_URL).respond(content=make_tarball(files, hostile=False))
    job = _queued_job(db)

    original = IndexRun._after_batch

    def cancel_after_first_batch(self, session, job_row, totals, total):  # type: ignore[no-untyped-def]
        session.execute(
            Job.__table__.update().where(Job.id == job_row.id).values(cancel_requested=True)
        )
        session.commit()
        original(self, session, job_row, totals, total)

    monkeypatch.setattr(IndexRun, "_after_batch", cancel_after_first_batch)
    _run(session_factory, job.id)

    db.expire_all()
    job = db.get(Job, job.id)
    assert job.status == "cancelled"
    assert db.get(RepositorySnapshot, job.snapshot_id).status == "cancelled"
    assert _counts(db)["files"] == 0
    assert pipeline.BATCH_FILES < len(files)


@respx.mock
def test_non_runnable_job_is_skipped(session_factory, db: Session) -> None:
    route = respx.get(TARBALL_URL).respond(content=make_tarball())
    job = _queued_job(db)
    job.status = "cancelled"
    db.commit()
    _run(session_factory, job.id)
    assert not route.called
