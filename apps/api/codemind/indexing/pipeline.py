"""The index_snapshot job: fetch a pinned commit, filter, parse, chunk and store it.

Each run first deletes whatever an earlier attempt stored for the snapshot, so retries and manual
re-indexes rewrite rows instead of duplicating them.

While it runs, a heartbeat thread marks the job alive (indexing/recovery.py). If the job is reaped
anyway (marked failed as stale while this worker was still going), the run notices at its next
checkpoint and stops without writing, so it never mixes rows with the job that replaced it.
"""

import logging
import tempfile
import time
import uuid
from collections import Counter
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from sqlalchemy import delete, func, insert, select
from sqlalchemy.orm import Session, sessionmaker

from codemind.core.config import Settings
from codemind.core.errors import CodemindError
from codemind.db.base import utcnow
from codemind.db.models import (
    Chunk,
    File,
    FileContent,
    Job,
    JobStatus,
    RepositorySnapshot,
    SnapshotStatus,
    Symbol,
)
from codemind.indexing.archive import ArchiveLimits, ScannedFile, count_entries, scan_archive
from codemind.indexing.chunking import chunk_file
from codemind.indexing.recovery import Heartbeat
from codemind.parsing.treesitter import SUPPORTED_LANGUAGES, ParsedSymbol, parse_symbols
from codemind.repositories.github import GitHubClient

log = logging.getLogger(__name__)

BATCH_FILES = 200
# Reasons that mean some indexable code was skipped, so the snapshot is "partial".
PARTIAL_REASONS = ("too_large", "limit_reached")


class JobCancelled(Exception):
    pass


class JobLost(Exception):
    """The job is no longer running in the database: it was reaped as stale."""


@dataclass
class _Totals:
    files: int = 0
    included: int = 0
    chunks: int = 0
    symbols: int = 0
    excluded: Counter[str] = field(default_factory=Counter)
    parse_error_paths: list[str] = field(default_factory=list)
    truncated: bool = False


class IndexRun:
    def __init__(
        self,
        job_id: uuid.UUID,
        *,
        session_factory: sessionmaker[Session],
        github_factory: Callable[[], GitHubClient],
        settings: Settings,
        final_attempt: bool = True,
    ) -> None:
        self.job_id = job_id
        self.session_factory = session_factory
        self.github_factory = github_factory
        self.settings = settings
        self.final_attempt = final_attempt
        self.deadline = time.monotonic() + settings.max_job_seconds
        self._last_progress = 0.0

    def run(self) -> None:
        with self.session_factory() as db:
            # Locked so a concurrent reap cannot interleave with taking the job over.
            job = db.get(Job, self.job_id, with_for_update=True)
            if job is None or job.status not in (JobStatus.QUEUED, JobStatus.RUNNING):
                log.info("job %s is not runnable, skipping", self.job_id)
                db.rollback()
                return
            snapshot = db.get(RepositorySnapshot, job.snapshot_id)
            assert snapshot is not None
            try:
                self._run(db, job, snapshot)
            except JobLost:
                db.rollback()
                log.warning("job %s was marked failed while running, stopping", self.job_id)

    def _run(self, db: Session, job: Job, snapshot: RepositorySnapshot) -> None:
        try:
            job.status, job.stage = JobStatus.RUNNING, "resolve"
            job.attempt += 1
            job.started_at = job.started_at or utcnow()
            job.heartbeat_at = func.now()
            job.error_code = job.error_message = None
            snapshot.status = SnapshotStatus.FETCHING
            db.commit()
            self._checkpoint(db, job)
            with Heartbeat(job.id, self.session_factory, self.settings.job_heartbeat_seconds):
                self._index(db, job, snapshot)
        except JobLost:
            raise
        except JobCancelled:
            db.rollback()
            self._finish(
                db, job, snapshot, JobStatus.CANCELLED, SnapshotStatus.CANCELLED, clear=True
            )
        except CodemindError as exc:
            db.rollback()
            self._fail(db, job, snapshot, exc)
        except Exception as exc:
            db.rollback()
            log.exception("index job %s crashed", self.job_id)
            self._fail(
                db,
                job,
                snapshot,
                CodemindError("internal_error", "Indexing failed unexpectedly.", retryable=True),
                cause=exc,
            )

    def _index(self, db: Session, job: Job, snapshot: RepositorySnapshot) -> None:
        repo = snapshot.repository
        with tempfile.TemporaryDirectory(prefix="codemind-") as tmp:
            archive = Path(tmp) / "repo.tar.gz"
            self._stage(db, job, "fetch", 0)
            with self.github_factory() as gh:
                gh.download_tarball(repo.owner, repo.name, snapshot.commit_sha, archive)
            self._check_deadline()
            self._checkpoint(db, job)

            total_entries = count_entries(archive)
            snapshot.status = SnapshotStatus.INDEXING
            self._clear(db, snapshot.id)
            self._stage(db, job, "filter", 0)

            totals = _Totals()
            limits = ArchiveLimits(self.settings.max_repository_files, self.settings.max_file_bytes)
            batch: list[ScannedFile] = []
            job.stage = "parse"
            for entry in scan_archive(archive, limits):
                batch.append(entry)
                if len(batch) >= BATCH_FILES:
                    self._store(db, snapshot.id, batch, totals)
                    batch = []
                    self._after_batch(db, job, totals, total_entries)
            if batch:
                self._store(db, snapshot.id, batch, totals)
            totals.truncated = totals.files < total_entries
            self._stage(db, job, "finalize", 99)

        snapshot.stats = {
            "files_total": totals.files,
            "files_included": totals.included,
            "files_excluded": dict(totals.excluded),
            "chunks": totals.chunks,
            "symbols": totals.symbols,
            "parse_errors": len(totals.parse_error_paths),
        }
        snapshot.warnings = _warnings(totals)
        partial = bool(snapshot.warnings)
        snapshot.indexed_at = utcnow()
        self._finish(
            db,
            job,
            snapshot,
            JobStatus.SUCCEEDED,
            SnapshotStatus.PARTIAL if partial else SnapshotStatus.READY,
        )

    def _store(
        self, db: Session, snapshot_id: uuid.UUID, batch: list[ScannedFile], totals: _Totals
    ) -> None:
        file_rows: list[dict[str, Any]] = []
        content_rows: list[dict[str, Any]] = []
        symbol_rows: list[dict[str, Any]] = []
        chunk_rows: list[dict[str, Any]] = []
        for entry in batch:
            totals.files += 1
            file_id = uuid.uuid4()
            file_rows.append(
                {
                    "id": file_id,
                    "snapshot_id": snapshot_id,
                    "path": entry.path,
                    "language": entry.language,
                    "size_bytes": entry.size_bytes,
                    "content_hash": entry.content_hash,
                    "line_count": entry.line_count,
                    "excluded_reason": entry.excluded_reason,
                }
            )
            if entry.excluded_reason or entry.text is None:
                totals.excluded[entry.excluded_reason or "unsupported"] += 1
                continue
            totals.included += 1
            content_rows.append({"file_id": file_id, "text": entry.text})

            symbols: list[ParsedSymbol] | None = None
            if entry.language in SUPPORTED_LANGUAGES:
                result = parse_symbols(entry.language, entry.text)
                if result.has_errors:
                    totals.parse_error_paths.append(entry.path)
                else:
                    symbols = result.symbols
                    symbol_rows.extend(_symbol_rows(symbols, file_id, snapshot_id))
            for ordinal, chunk in enumerate(chunk_file(entry.text, symbols)):
                chunk_rows.append(
                    {
                        "id": uuid.uuid4(),
                        "file_id": file_id,
                        "snapshot_id": snapshot_id,
                        "ordinal": ordinal,
                        "start_line": chunk.start_line,
                        "end_line": chunk.end_line,
                        "kind": chunk.kind,
                        "symbol_name": chunk.symbol_name,
                        "text": chunk.text,
                        "token_estimate": chunk.token_estimate,
                    }
                )
        totals.symbols += len(symbol_rows)
        totals.chunks += len(chunk_rows)
        for model, rows in (
            (File, file_rows),
            (FileContent, content_rows),
            (Symbol, symbol_rows),
            (Chunk, chunk_rows),
        ):
            if rows:
                db.execute(insert(model), rows)
        db.commit()

    def _after_batch(self, db: Session, job: Job, totals: _Totals, total_entries: int) -> None:
        self._check_deadline()
        self._checkpoint(db, job)
        if time.monotonic() - self._last_progress >= 1.0:
            pct = 5 + int(90 * totals.files / max(total_entries, 1))
            self._stage(db, job, "store", min(pct, 95))

    def _stage(self, db: Session, job: Job, stage: str, progress: int) -> None:
        job.stage, job.progress = stage, progress
        db.commit()
        self._last_progress = time.monotonic()

    def _check_deadline(self) -> None:
        if time.monotonic() > self.deadline:
            raise CodemindError("job_timeout", "Indexing took longer than the allowed time.")

    def _checkpoint(self, db: Session, job: Job) -> None:
        status, cancel_requested = db.execute(
            select(Job.status, Job.cancel_requested).where(Job.id == job.id)
        ).one()
        if status != JobStatus.RUNNING:
            raise JobLost
        if cancel_requested:
            raise JobCancelled

    def _claim(self, db: Session, job: Job) -> None:
        """Lock the job row until the next commit, after checking this run still owns it."""
        status = db.scalar(select(Job.status).where(Job.id == job.id).with_for_update())
        if status != JobStatus.RUNNING:
            raise JobLost

    def _clear(self, db: Session, snapshot_id: uuid.UUID) -> None:
        db.execute(delete(File).where(File.snapshot_id == snapshot_id))

    def _fail(
        self,
        db: Session,
        job: Job,
        snapshot: RepositorySnapshot,
        exc: CodemindError,
        cause: Exception | None = None,
    ) -> None:
        self._claim(db, job)
        job.error_code, job.error_message = exc.code, exc.message
        if exc.retryable and not self.final_attempt:
            job.status, job.stage = JobStatus.QUEUED, "retry"
            snapshot.status = SnapshotStatus.QUEUED
            db.commit()
            raise exc from cause  # RQ schedules the retry
        self._finish(db, job, snapshot, JobStatus.FAILED, SnapshotStatus.FAILED, clear=True)

    def _finish(
        self,
        db: Session,
        job: Job,
        snapshot: RepositorySnapshot,
        job_status: JobStatus,
        snapshot_status: SnapshotStatus,
        *,
        clear: bool = False,
    ) -> None:
        self._claim(db, job)
        if clear:
            self._clear(db, snapshot.id)
        job.status, snapshot.status = job_status, snapshot_status
        if job_status == JobStatus.SUCCEEDED:
            job.progress = 100
        job.finished_at = utcnow()
        db.commit()


def _symbol_rows(
    symbols: list[ParsedSymbol], file_id: uuid.UUID, snapshot_id: uuid.UUID
) -> list[dict[str, Any]]:
    ids = {id(s): uuid.uuid4() for s in symbols}
    return [
        {
            "id": ids[id(s)],
            "file_id": file_id,
            "snapshot_id": snapshot_id,
            "parent_symbol_id": ids.get(id(s.parent)) if s.parent else None,
            "name": s.name,
            "qualified_name": s.qualified_name,
            "kind": s.kind,
            "start_line": s.start_line,
            "end_line": s.end_line,
        }
        for s in symbols  # source order, so parents are inserted before children
    ]


def _warnings(totals: _Totals) -> list[dict[str, Any]]:
    warnings: list[dict[str, Any]] = []
    for reason in PARTIAL_REASONS:
        if count := totals.excluded.get(reason):
            warnings.append({"code": f"files_{reason}", "count": count})
    if totals.parse_error_paths:
        warnings.append(
            {
                "code": "parse_errors",
                "count": len(totals.parse_error_paths),
                "message": "These files had syntax errors and were indexed as plain text.",
                "paths": totals.parse_error_paths[:20],
            }
        )
    if totals.truncated:
        warnings.append(
            {"code": "archive_truncated", "message": "The repository has too many files to list."}
        )
    return warnings
