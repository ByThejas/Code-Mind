import uuid
from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class ORMModel(BaseModel):
    model_config = ConfigDict(from_attributes=True)


class ImportRepositoryRequest(BaseModel):
    url: str = Field(max_length=500, examples=["https://github.com/owner/repo"])
    ref: str | None = Field(default=None, max_length=255, description="Branch, tag or commit SHA.")


class IndexRepositoryRequest(BaseModel):
    ref: str | None = Field(default=None, max_length=255)
    force: bool = Field(default=False, description="Re-index even if this commit is indexed.")


class SnapshotOut(ORMModel):
    id: uuid.UUID
    commit_sha: str
    requested_ref: str | None
    status: str
    stats: dict[str, Any]
    warnings: list[dict[str, Any]]
    created_at: datetime
    indexed_at: datetime | None


class RepositoryOut(ORMModel):
    id: uuid.UUID
    provider: str
    owner: str
    name: str
    default_branch: str
    visibility: str
    created_at: datetime
    latest_snapshot: SnapshotOut | None = None


class JobOut(ORMModel):
    id: uuid.UUID
    snapshot_id: uuid.UUID | None
    type: str
    status: str
    stage: str | None
    progress: int
    attempt: int
    cancel_requested: bool
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class IndexResponse(BaseModel):
    repository: RepositoryOut
    snapshot: SnapshotOut
    job: JobOut | None


class RepositoryList(BaseModel):
    items: list[RepositoryOut]
    next_cursor: str | None


class FileOut(ORMModel):
    path: str
    language: str | None
    size_bytes: int
    line_count: int
    excluded_reason: str | None


class FileList(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    items: list[FileOut]
    next_cursor: str | None


class FileContentOut(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    path: str
    language: str | None
    start_line: int
    end_line: int
    line_count: int
    text: str
