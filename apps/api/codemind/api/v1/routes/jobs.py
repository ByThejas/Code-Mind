import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from redis.exceptions import RedisError
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_current_workspace, get_db
from codemind.core.config import settings
from codemind.core.errors import CodemindError, NotFoundError
from codemind.db.base import utcnow
from codemind.db.models import Job, JobStatus, RepositorySnapshot, SnapshotStatus
from codemind.indexing.queue import cancel_queued_job
from codemind.indexing.recovery import reap_stale_jobs
from codemind.schemas.ingestion import JobOut

log = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Jobs"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]


@router.get("/{job_id}", response_model=JobOut)
def get_job(job_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> Job:
    job = _owned_job(db, workspace_id, job_id)
    if job.status == JobStatus.RUNNING and reap_stale_jobs(
        db, settings, snapshot_id=job.snapshot_id
    ):
        db.commit()
        db.refresh(job)
    return job


@router.post("/{job_id}/cancel", response_model=JobOut, status_code=status.HTTP_202_ACCEPTED)
def cancel_job(job_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> Job:
    """Cancel a job. A queued job stops at once; a running one stops at its next checkpoint."""
    job = _owned_job(db, workspace_id, job_id, for_update=True)
    if job.status == JobStatus.QUEUED:
        job.status, job.cancel_requested, job.finished_at = JobStatus.CANCELLED, True, utcnow()
        snapshot = db.get(RepositorySnapshot, job.snapshot_id)
        if snapshot is not None:
            snapshot.status = SnapshotStatus.CANCELLED
        db.commit()
        try:
            cancel_queued_job(str(job.id))
        except RedisError:  # harmless: the worker skips jobs that are no longer queued
            log.warning("could not remove job %s from the queue", job.id, exc_info=True)
    elif job.status == JobStatus.RUNNING:
        job.cancel_requested = True
        db.commit()
    else:
        raise CodemindError(
            "job_not_cancellable", f"The job has already {job.status}.", status_code=409
        )
    return job


def _owned_job(
    db: Session, workspace_id: uuid.UUID, job_id: uuid.UUID, *, for_update: bool = False
) -> Job:
    job = db.get(Job, job_id, with_for_update=for_update)
    if job is None or job.workspace_id != workspace_id:
        raise NotFoundError("job_not_found", "Job not found.")
    return job
