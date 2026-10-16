import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status
from sqlalchemy import select, tuple_
from sqlalchemy.dialects.postgresql import distinct_on
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_current_workspace, get_db, get_enqueue, get_github
from codemind.core.config import settings
from codemind.core.errors import NotFoundError
from codemind.db.models import Repository, RepositorySnapshot
from codemind.repositories.github import GitHubClient
from codemind.repositories.service import (
    Enqueue,
    IndexRequest,
    import_repository,
    request_index,
)
from codemind.schemas.ingestion import (
    ImportRepositoryRequest,
    IndexRepositoryRequest,
    IndexResponse,
    JobOut,
    RepositoryList,
    RepositoryOut,
    SnapshotOut,
)

router = APIRouter(prefix="/repositories", tags=["Repositories"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]
GitHub = Annotated[GitHubClient, Depends(get_github)]
EnqueueDep = Annotated[Enqueue, Depends(get_enqueue)]


@router.post(
    "",
    response_model=IndexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "That commit is already indexed; no job was queued."}},
)
def create_repository(
    body: ImportRepositoryRequest,
    response: Response,
    db: Db,
    workspace_id: WorkspaceId,
    github: GitHub,
    enqueue: EnqueueDep,
) -> IndexResponse:
    """Register a public GitHub repository and queue indexing of a pinned commit."""
    result = import_repository(
        db, workspace_id, body.url, body.ref, github=github, enqueue=enqueue, settings=settings
    )
    return _index_response(result, response)


@router.get("", response_model=RepositoryList)
def list_repositories(
    db: Db,
    workspace_id: WorkspaceId,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: uuid.UUID | None = None,
) -> RepositoryList:
    query = select(Repository).where(Repository.workspace_id == workspace_id)
    if cursor is not None:
        anchor = db.get(Repository, cursor)
        if anchor is not None and anchor.workspace_id == workspace_id:
            query = query.where(
                tuple_(Repository.created_at, Repository.id) > (anchor.created_at, anchor.id)
            )
    repos = list(db.scalars(query.order_by(Repository.created_at, Repository.id).limit(limit + 1)))
    page, more = repos[:limit], len(repos) > limit
    latest = _latest_snapshots(db, [r.id for r in page])
    return RepositoryList(
        items=[_repository_out(r, latest.get(r.id)) for r in page],
        next_cursor=str(page[-1].id) if more else None,
    )


@router.get("/{repository_id}", response_model=RepositoryOut)
def get_repository(repository_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> RepositoryOut:
    repo = get_owned_repository(db, workspace_id, repository_id)
    return _repository_out(repo, _latest_snapshots(db, [repo.id]).get(repo.id))


@router.post(
    "/{repository_id}/index",
    response_model=IndexResponse,
    status_code=status.HTTP_202_ACCEPTED,
    responses={200: {"description": "That commit is already indexed; no job was queued."}},
)
def index_repository(
    repository_id: uuid.UUID,
    response: Response,
    db: Db,
    workspace_id: WorkspaceId,
    github: GitHub,
    enqueue: EnqueueDep,
    body: IndexRepositoryRequest | None = None,
) -> IndexResponse:
    """Index the default branch head, or `ref`. Idempotent per commit unless `force` is set."""
    body = body or IndexRepositoryRequest()
    repo = get_owned_repository(db, workspace_id, repository_id)
    result = request_index(
        db, repo, body.ref, github=github, enqueue=enqueue, settings=settings, force=body.force
    )
    return _index_response(result, response)


def get_owned_repository(
    db: Session, workspace_id: uuid.UUID, repository_id: uuid.UUID
) -> Repository:
    repo = db.get(Repository, repository_id)
    if repo is None or repo.workspace_id != workspace_id:
        raise NotFoundError("repository_not_found", "Repository not found.")
    return repo


def _latest_snapshots(
    db: Session, repository_ids: list[uuid.UUID]
) -> dict[uuid.UUID, RepositorySnapshot]:
    if not repository_ids:
        return {}
    rows = db.scalars(
        select(RepositorySnapshot)
        .where(RepositorySnapshot.repository_id.in_(repository_ids))
        .ext(distinct_on(RepositorySnapshot.repository_id))
        .order_by(RepositorySnapshot.repository_id, RepositorySnapshot.created_at.desc())
    )
    return {s.repository_id: s for s in rows}


def _repository_out(repo: Repository, snapshot: RepositorySnapshot | None) -> RepositoryOut:
    out = RepositoryOut.model_validate(repo)
    out.latest_snapshot = SnapshotOut.model_validate(snapshot) if snapshot else None
    return out


def _index_response(result: IndexRequest, response: Response) -> IndexResponse:
    if result.job is None:
        response.status_code = status.HTTP_200_OK
    return IndexResponse(
        repository=_repository_out(result.repository, result.snapshot),
        snapshot=SnapshotOut.model_validate(result.snapshot),
        job=JobOut.model_validate(result.job) if result.job else None,
    )
