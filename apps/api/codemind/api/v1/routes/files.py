import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_current_workspace, get_db
from codemind.api.v1.routes.repositories import get_owned_repository
from codemind.core.errors import CodemindError, NotFoundError
from codemind.db.models import SERVABLE_SNAPSHOT_STATUSES, File, FileContent, RepositorySnapshot
from codemind.schemas.ingestion import FileContentOut, FileList, FileOut

router = APIRouter(prefix="/repositories/{repository_id}/files", tags=["Files"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]

MAX_CONTENT_LINES = 2000


@router.get("", response_model=FileList)
def list_files(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    snapshot_id: uuid.UUID | None = None,
    path_prefix: Annotated[str | None, Query(max_length=1000)] = None,
    include_excluded: bool = False,
    cursor: Annotated[str | None, Query(max_length=4000, description="Last path seen.")] = None,
    limit: Annotated[int, Query(ge=1, le=500)] = 200,
) -> FileList:
    snapshot = _servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    query = select(File).where(File.snapshot_id == snapshot.id)
    if path_prefix:
        escaped = path_prefix.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        query = query.where(File.path.like(f"{escaped}%", escape="\\"))
    if not include_excluded:
        query = query.where(File.excluded_reason.is_(None))
    if cursor:
        query = query.where(File.path > cursor)
    files = list(db.scalars(query.order_by(File.path).limit(limit + 1)))
    page = files[:limit]
    return FileList(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        items=[FileOut.model_validate(f) for f in page],
        next_cursor=page[-1].path if len(files) > limit else None,
    )


@router.get("/content", response_model=FileContentOut)
def read_file(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    path: Annotated[str, Query(max_length=4000)],
    snapshot_id: uuid.UUID | None = None,
    start_line: Annotated[int, Query(ge=1)] = 1,
    end_line: Annotated[int | None, Query(ge=1)] = None,
) -> FileContentOut:
    """Return a line range of an indexed file, at most 2,000 lines per request."""
    snapshot = _servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    file = db.scalar(select(File).where(File.snapshot_id == snapshot.id, File.path == path))
    if file is None:
        raise NotFoundError("file_not_found", "No file with that path in this snapshot.")
    if file.excluded_reason:
        raise CodemindError(
            "file_excluded", f"This file was not indexed ({file.excluded_reason}).", status_code=409
        )
    content = db.get(FileContent, file.id)
    lines = (content.text if content else "").splitlines(keepends=True)
    if end_line is not None and end_line < start_line:
        raise CodemindError(
            "invalid_line_range", "end_line must be >= start_line.", status_code=422
        )
    last = min(end_line or len(lines), start_line + MAX_CONTENT_LINES - 1, len(lines))
    first = min(start_line, last + 1)
    return FileContentOut(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        path=file.path,
        language=file.language,
        start_line=first,
        end_line=last,
        line_count=len(lines),
        text="".join(lines[first - 1 : last]),
    )


def _servable_snapshot(
    db: Session, workspace_id: uuid.UUID, repository_id: uuid.UUID, snapshot_id: uuid.UUID | None
) -> RepositorySnapshot:
    repo = get_owned_repository(db, workspace_id, repository_id)
    query = select(RepositorySnapshot).where(
        RepositorySnapshot.repository_id == repo.id,
        RepositorySnapshot.status.in_(SERVABLE_SNAPSHOT_STATUSES),
    )
    if snapshot_id is not None:
        query = query.where(RepositorySnapshot.id == snapshot_id)
    snapshot = db.scalar(query.order_by(RepositorySnapshot.indexed_at.desc().nulls_last()).limit(1))
    if snapshot is None:
        raise CodemindError(
            "snapshot_not_ready",
            "This repository has no indexed snapshot yet."
            if snapshot_id is None
            else "That snapshot is not indexed.",
            status_code=409,
        )
    return snapshot
