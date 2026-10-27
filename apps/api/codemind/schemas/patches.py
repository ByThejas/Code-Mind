import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

# What the model must return when asked for a patch: exact find-and-replace edits.


class PatchEditDraft(BaseModel):
    path: str = Field(max_length=1000)
    old_text: str = Field(max_length=20_000)
    new_text: str = Field(max_length=40_000)


class PatchDraft(BaseModel):
    summary: str = Field(max_length=2000)
    edits: list[PatchEditDraft] = Field(max_length=20)
    limitations: list[str] = Field(max_length=10)


# API


class PatchEditIn(BaseModel):
    path: str = Field(max_length=1000, description="Repository-relative path, forward slashes.")
    old_text: str = Field(
        max_length=20_000,
        description="Exact current text to replace; it must appear once. Empty creates the file.",
    )
    new_text: str = Field(max_length=40_000)


class PatchRequest(BaseModel):
    instruction: str = Field(min_length=3, max_length=2000, description="The change you want.")
    edits: list[PatchEditIn] | None = Field(
        default=None,
        max_length=20,
        description="Your own edits. When given, no model is called and they are only checked.",
    )


class PatchIssue(BaseModel):
    code: str
    message: str
    edit_index: int | None
    path: str | None


class PatchValidation(BaseModel):
    errors: list[PatchIssue] = []
    warnings: list[PatchIssue] = []


class PatchFile(BaseModel):
    path: str
    status: Literal["added", "modified"]
    additions: int
    deletions: int


class PatchOut(BaseModel):
    id: uuid.UUID
    investigation_id: uuid.UUID
    repository_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    instruction: str
    source: Literal["model", "manual"]
    status: str
    summary: str | None
    limitations: list[str]
    edits: list[PatchEditIn]
    files: list[PatchFile]
    diff: str | None
    diff_sha256: str | None
    validation: PatchValidation
    error_code: str | None
    error_message: str | None
    metadata: dict[str, Any]
    created_at: datetime
    decided_at: datetime | None
    decided_by: str | None
    exported_at: datetime | None


class PatchSummary(BaseModel):
    id: uuid.UUID
    instruction: str
    source: Literal["model", "manual"]
    status: str
    files: list[PatchFile]
    created_at: datetime


class PatchList(BaseModel):
    items: list[PatchSummary]


class PatchApproval(BaseModel):
    diff_sha256: str = Field(
        pattern=r"^[0-9a-f]{64}$",
        description="SHA-256 of the diff you reviewed. Approval fails if the patch differs.",
    )


class PatchRejection(BaseModel):
    reason: str | None = Field(default=None, max_length=2000)


class PatchExport(BaseModel):
    patch_id: uuid.UUID
    filename: str
    diff: str
    diff_sha256: str
    commit_sha: str


class TestRunRequest(BaseModel):
    __test__ = False

    profile: str = Field(
        default="pytest",
        max_length=40,
        description="A server-defined test profile: pytest, node-test.",
    )


class TestRunOut(BaseModel):
    __test__ = False

    id: uuid.UUID
    patch_id: uuid.UUID
    profile: str
    diff_sha256: str
    status: str
    image: str
    command: list[str]
    limits: dict[str, Any]
    exit_code: int | None
    duration_ms: int | None
    output: str | None
    output_truncated: bool
    workspace_stats: dict[str, Any]
    error_code: str | None
    error_message: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TestRunList(BaseModel):
    __test__ = False

    items: list[TestRunOut]
