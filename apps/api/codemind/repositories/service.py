"""Repository import and re-index: validate, pin a commit, and make sure one index job exists for it."""

import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from codemind.core.config import Settings
from codemind.core.errors import CodemindError
from codemind.db.base import utcnow
from codemind.db.models import (
    ACTIVE_JOB_STATUSES,
    SERVABLE_SNAPSHOT_STATUSES,
    Job,
    JobStatus,
    Repository,
    RepositorySnapshot,
    SnapshotStatus,
)
from codemind.indexing.recovery import reap_stale_jobs
from codemind.repositories.github import GitHubClient, parse_repository_url

Enqueue = Callable[[str], None]


@dataclass
class IndexRequest:
    repository: Repository
    snapshot: RepositorySnapshot
    job: Job | None  # None when the commit is already indexed


def import_repository(
    db: Session,
    workspace_id: uuid.UUID,
    url: str,
    ref: str | None,
    *,
    github: GitHubClient,
    enqueue: Enqueue,
    settings: Settings,
) -> IndexRequest:
    target = parse_repository_url(url)
    meta = github.get_repository(target.owner, target.name)
    if meta.private:
        raise CodemindError(
            "repository_private", "Only public repositories are supported for now.", status_code=403
        )
    if meta.size_kb * 1024 > settings.max_index_bytes:
        raise CodemindError(
            "repository_too_large",
            "The repository is larger than the indexing limit.",
            status_code=413,
        )

    repo = db.scalar(
        select(Repository).where(
            Repository.workspace_id == workspace_id,
            Repository.provider == "github",
            func.lower(Repository.owner) == meta.owner.lower(),
            func.lower(Repository.name) == meta.name.lower(),
        )
    )
    if repo is None:
        repo = Repository(
            workspace_id=workspace_id,
            provider="github",
            owner=meta.owner,
            name=meta.name,
            default_branch=meta.default_branch,
            visibility="public",
        )
        db.add(repo)
        try:
            db.commit()
        except IntegrityError:  # a concurrent import created it first
            db.rollback()
            return import_repository(
                db, workspace_id, url, ref, github=github, enqueue=enqueue, settings=settings
            )
    elif repo.default_branch != meta.default_branch:
        repo.default_branch = meta.default_branch
        db.commit()

    return request_index(
        db,
        repo,
        ref or target.ref,
        github=github,
        enqueue=enqueue,
        settings=settings,
        force=False,
    )


def request_index(
    db: Session,
    repo: Repository,
    ref: str | None,
    *,
    github: GitHubClient,
    enqueue: Enqueue,
    settings: Settings,
    force: bool,
) -> IndexRequest:
    requested_ref = ref or repo.default_branch
    sha = github.resolve_ref(repo.owner, repo.name, requested_ref)

    db.execute(
        pg_insert(RepositorySnapshot)
        .values(
            id=uuid.uuid4(),
            repository_id=repo.id,
            commit_sha=sha,
            requested_ref=requested_ref,
            status=SnapshotStatus.QUEUED,
            stats={},
            warnings=[],
            created_at=utcnow(),
        )
        .on_conflict_do_nothing(index_elements=["repository_id", "commit_sha"])
    )
    db.commit()
    snapshot = db.scalars(
        select(RepositorySnapshot)
        .where(RepositorySnapshot.repository_id == repo.id, RepositorySnapshot.commit_sha == sha)
        .with_for_update()
    ).one()

    # A job left running by a dead worker would otherwise block this commit forever.
    reap_stale_jobs(db, settings, snapshot_id=snapshot.id)
    active = _active_job(db, snapshot.id)
    if active is not None:
        db.commit()
        return IndexRequest(repo, snapshot, active)
    if snapshot.status in SERVABLE_SNAPSHOT_STATUSES and not force:
        db.commit()
        return IndexRequest(repo, snapshot, None)

    job = Job(workspace_id=repo.workspace_id, snapshot_id=snapshot.id, status=JobStatus.QUEUED)
    snapshot.status = SnapshotStatus.QUEUED
    snapshot.requested_ref = requested_ref
    db.add(job)
    try:
        db.commit()
    except IntegrityError:  # another request queued a job for this snapshot first
        db.rollback()
        existing = _active_job(db, snapshot.id)
        assert existing is not None
        return IndexRequest(repo, snapshot, existing)

    try:
        enqueue(str(job.id))
    except Exception as exc:
        job.status, job.error_code = JobStatus.FAILED, "queue_unavailable"
        job.error_message = "The indexing queue is unavailable."
        job.finished_at = utcnow()
        snapshot.status = SnapshotStatus.FAILED
        db.commit()
        raise CodemindError(
            "queue_unavailable", "The indexing queue is unavailable.", status_code=503
        ) from exc
    return IndexRequest(repo, snapshot, job)


def _active_job(db: Session, snapshot_id: uuid.UUID) -> Job | None:
    return db.scalar(
        select(Job).where(Job.snapshot_id == snapshot_id, Job.status.in_(ACTIVE_JOB_STATUSES))
    )
