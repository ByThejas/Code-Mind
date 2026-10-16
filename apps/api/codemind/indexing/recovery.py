"""Recover jobs left `running` by a worker that died (OOM kill, lost machine, killed container).

A running job's worker refreshes `jobs.heartbeat_at` every `job_heartbeat_seconds`. When that stops
for `job_stale_seconds`, the job is marked failed and its snapshot cleared, which frees the
one-active-job-per-snapshot slot so the commit can be indexed again. Both sides use the database
clock, so clock skew between API and worker machines does not matter.
"""

import logging
import threading
import uuid
from datetime import timedelta
from typing import Self

from sqlalchemy import delete, func, update
from sqlalchemy.orm import Session, sessionmaker

from codemind.core.config import Settings
from codemind.db.models import File, Job, JobStatus, RepositorySnapshot, SnapshotStatus

log = logging.getLogger(__name__)

WORKER_LOST = "worker_lost"


def reap_stale_jobs(
    db: Session, settings: Settings, *, snapshot_id: uuid.UUID | None = None
) -> list[uuid.UUID]:
    """Fail running jobs whose worker stopped reporting in. Returns the ids of the jobs failed.

    The caller commits. A worker that comes back after being reaped notices at
    its next checkpoint that it no longer owns the job and stops without writing anything.
    """
    last_seen = func.coalesce(Job.heartbeat_at, Job.started_at, Job.created_at)
    stmt = (
        update(Job)
        .where(
            Job.status == JobStatus.RUNNING,
            last_seen < func.now() - timedelta(seconds=settings.job_stale_seconds),
        )
        .values(
            status=JobStatus.FAILED,
            error_code=WORKER_LOST,
            error_message="The indexing worker stopped responding.",
            finished_at=func.now(),
        )
        .returning(Job.id, Job.snapshot_id)
    )
    if snapshot_id is not None:
        stmt = stmt.where(Job.snapshot_id == snapshot_id)
    reaped = db.execute(stmt).all()
    if not reaped:
        return []

    snapshot_ids = [row.snapshot_id for row in reaped if row.snapshot_id is not None]
    if snapshot_ids:
        # Same end state as a job that failed in the pipeline: no half-written rows, never served.
        db.execute(delete(File).where(File.snapshot_id.in_(snapshot_ids)))
        db.execute(
            update(RepositorySnapshot)
            .where(RepositorySnapshot.id.in_(snapshot_ids))
            .values(status=SnapshotStatus.FAILED)
        )
    for row in reaped:
        log.warning(
            "job %s had no heartbeat for %ss, marked failed", row.id, settings.job_stale_seconds
        )
    return [row.id for row in reaped]


class Heartbeat:
    """Refresh a running job's heartbeat from a background thread until stopped.

    A thread (rather than writes from the pipeline loop) keeps the heartbeat alive through long
    single steps such as downloading a large tarball. It dies with the process, which is the point.
    """

    def __init__(
        self, job_id: uuid.UUID, session_factory: sessionmaker[Session], interval: float
    ) -> None:
        self.job_id = job_id
        self.session_factory = session_factory
        self.interval = interval
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._loop, name=f"heartbeat-{job_id}", daemon=True)

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc: object) -> None:
        self._stop.set()
        self._thread.join()

    def _loop(self) -> None:
        while not self._stop.wait(self.interval):
            try:
                self.beat()
            except Exception:  # a missed beat is fine; staleness needs many in a row
                log.warning("heartbeat for job %s failed", self.job_id, exc_info=True)

    def beat(self) -> None:
        with self.session_factory() as db:
            db.execute(
                update(Job)
                .where(Job.id == self.job_id, Job.status == JobStatus.RUNNING)
                .values(heartbeat_at=func.now())
            )
            db.commit()
