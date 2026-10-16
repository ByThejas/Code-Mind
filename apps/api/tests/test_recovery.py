import time
import uuid
from datetime import timedelta

import respx
from fastapi.testclient import TestClient
from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, sessionmaker

from codemind.db.models import File, Job, RepositorySnapshot
from codemind.indexing import pipeline
from codemind.indexing.pipeline import IndexRun
from codemind.indexing.recovery import Heartbeat, reap_stale_jobs
from tests.fixtures import make_tarball
from tests.test_api import _work
from tests.test_pipeline import SETTINGS, TARBALL_URL, _counts, _queued_job, _run

STALE = timedelta(seconds=SETTINGS.job_stale_seconds + 60)


def _mark_running(db: Session, job_id: uuid.UUID, *, silent_for: timedelta | None) -> None:
    """Put a job in the state a worker leaves it in, last heard from `silent_for` ago."""
    heartbeat = None if silent_for is None else func.now() - silent_for
    db.execute(
        update(Job)
        .where(Job.id == job_id)
        .values(status="running", started_at=func.now() - STALE, heartbeat_at=heartbeat)
    )
    db.commit()


def _add_file(db: Session, snapshot_id: uuid.UUID) -> None:
    db.add(File(snapshot_id=snapshot_id, path="half.py", size_bytes=1, line_count=1))
    db.commit()


def test_stale_running_job_is_failed_and_its_rows_cleared(db: Session) -> None:
    job = _queued_job(db)
    _mark_running(db, job.id, silent_for=STALE)
    db.execute(
        update(RepositorySnapshot)
        .where(RepositorySnapshot.id == job.snapshot_id)
        .values(status="indexing")
    )
    _add_file(db, job.snapshot_id)

    assert reap_stale_jobs(db, SETTINGS) == [job.id]
    db.commit()

    db.expire_all()
    job = db.get(Job, job.id)
    assert (job.status, job.error_code) == ("failed", "worker_lost")
    assert job.finished_at is not None
    assert db.get(RepositorySnapshot, job.snapshot_id).status == "failed"
    assert _counts(db)["files"] == 0


def test_live_and_queued_jobs_are_left_alone(db: Session) -> None:
    job = _queued_job(db)
    assert reap_stale_jobs(db, SETTINGS) == []  # queued

    _mark_running(db, job.id, silent_for=timedelta(seconds=5))
    assert reap_stale_jobs(db, SETTINGS) == []
    db.expire_all()
    assert db.get(Job, job.id).status == "running"


def test_job_that_never_beat_is_judged_by_its_start_time(db: Session) -> None:
    job = _queued_job(db)
    _mark_running(db, job.id, silent_for=None)  # started long ago, no heartbeat recorded
    assert reap_stale_jobs(db, SETTINGS) == [job.id]


def test_reap_can_be_scoped_to_one_snapshot(db: Session) -> None:
    job = _queued_job(db)
    _mark_running(db, job.id, silent_for=STALE)
    assert reap_stale_jobs(db, SETTINGS, snapshot_id=uuid.uuid4()) == []
    assert reap_stale_jobs(db, SETTINGS, snapshot_id=job.snapshot_id) == [job.id]


def test_heartbeat_refreshes_only_running_jobs(session_factory, db: Session) -> None:
    job = _queued_job(db)
    _mark_running(db, job.id, silent_for=STALE)
    with Heartbeat(job.id, session_factory, interval=0.01):
        time.sleep(0.2)
    assert reap_stale_jobs(db, SETTINGS) == []

    db.execute(update(Job).where(Job.id == job.id).values(status="failed", heartbeat_at=None))
    db.commit()
    Heartbeat(job.id, session_factory, interval=1).beat()
    db.expire_all()
    assert db.get(Job, job.id).heartbeat_at is None


@respx.mock
def test_run_records_a_heartbeat(session_factory, db: Session) -> None:
    respx.get(TARBALL_URL).respond(content=make_tarball())
    job = _queued_job(db)
    _run(session_factory, job.id)
    db.expire_all()
    assert db.get(Job, job.id).heartbeat_at is not None


@respx.mock
def test_reaped_run_stops_without_writing(session_factory, db: Session, monkeypatch) -> None:
    """A worker that was only slow, not dead, must not keep writing once its job is reaped."""
    files = {f"src/m{i}.py": f"def f{i}():\n    return {i}\n".encode() for i in range(450)}
    respx.get(TARBALL_URL).respond(content=make_tarball(files, hostile=False))
    job = _queued_job(db)
    original = IndexRun._after_batch

    def reaped_after_first_batch(self, session, job_row, totals, total):  # type: ignore[no-untyped-def]
        with session_factory() as other:
            _mark_running(other, job_row.id, silent_for=STALE)
            assert reap_stale_jobs(other, SETTINGS) == [job_row.id]
            other.commit()
        original(self, session, job_row, totals, total)

    monkeypatch.setattr(IndexRun, "_after_batch", reaped_after_first_batch)
    _run(session_factory, job.id)

    db.expire_all()
    job = db.get(Job, job.id)
    assert (job.status, job.error_code) == ("failed", "worker_lost")
    assert db.get(RepositorySnapshot, job.snapshot_id).status == "failed"
    assert _counts(db)["files"] == 0
    assert pipeline.BATCH_FILES < len(files)


def test_reindex_after_worker_crash(
    client: TestClient, session_factory: sessionmaker[Session], db: Session, enqueued: list[str]
) -> None:
    first = client.post("/api/v1/repositories", json={"url": "owner/repo"}).json()["job"]["id"]
    _mark_running(db, uuid.UUID(first), silent_for=timedelta(seconds=5))

    # While the worker is alive, importing again joins its job.
    again = client.post("/api/v1/repositories", json={"url": "owner/repo"})
    assert again.json()["job"]["id"] == first

    # The worker dies: the job stops beating and the next import replaces it.
    _mark_running(db, uuid.UUID(first), silent_for=STALE)
    retry = client.post("/api/v1/repositories", json={"url": "owner/repo"})
    assert retry.status_code == 202
    second = retry.json()["job"]["id"]
    assert second != first
    assert enqueued == [first, second]

    lost = client.get(f"/api/v1/jobs/{first}").json()
    assert (lost["status"], lost["error_code"]) == ("failed", "worker_lost")

    _work(session_factory, enqueued)  # the dead job is skipped, the new one indexes
    assert client.get(f"/api/v1/jobs/{second}").json()["status"] == "succeeded"
    db.expire_all()
    assert db.scalar(select(func.count()).select_from(Job)) == 2


def test_get_job_reports_a_lost_worker(client: TestClient, db: Session) -> None:
    job_id = client.post("/api/v1/repositories", json={"url": "owner/repo"}).json()["job"]["id"]
    _mark_running(db, uuid.UUID(job_id), silent_for=STALE)
    body = client.get(f"/api/v1/jobs/{job_id}").json()
    assert (body["status"], body["error_code"]) == ("failed", "worker_lost")
