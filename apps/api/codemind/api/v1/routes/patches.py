"""Patch proposals: check, review, approve and export (docs/patch-design.md)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from codemind.api.dependencies import (
    get_answer_model,
    get_current_workspace,
    get_db,
    get_embedding_provider,
    get_enqueue_test_run,
)
from codemind.api.ratelimit import rate_limit
from codemind.api.v1.routes.files import servable_snapshot
from codemind.core.config import settings
from codemind.core.errors import NotFoundError
from codemind.db.models import Investigation, Patch, RepositorySnapshot, TestRun
from codemind.patches import service
from codemind.providers import AnswerModel, EmbeddingProvider
from codemind.sandbox import runs
from codemind.schemas.patches import (
    PatchApproval,
    PatchExport,
    PatchList,
    PatchOut,
    PatchRejection,
    PatchRequest,
    PatchSummary,
    TestRunList,
    TestRunOut,
    TestRunRequest,
)

router = APIRouter(tags=["Patches"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]
Embedder = Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)]
Model = Annotated[AnswerModel | None, Depends(get_answer_model)]
EnqueueTestRun = Annotated[runs.Enqueue, Depends(get_enqueue_test_run)]


@router.post(
    "/investigations/{investigation_id}/patches",
    response_model=PatchOut,
    status_code=status.HTTP_201_CREATED,
    dependencies=[Depends(rate_limit("model"))],
)
def create_patch(
    investigation_id: uuid.UUID,
    body: PatchRequest,
    db: Db,
    workspace_id: WorkspaceId,
    embedder: Embedder,
    answer_model: Model,
) -> PatchOut:
    """Propose a change and check it against the investigation's snapshot.

    Without `edits`, the local model writes the edits from retrieved code. With `edits`, they are
    only checked. Either way the patch is stored: `validated` with a diff ready for review, or
    `invalid` with the reason for each failed edit. Nothing is applied to any repository.
    """
    investigation = _owned_investigation(db, workspace_id, investigation_id)
    snapshot = servable_snapshot(
        db, workspace_id, investigation.repository_id, investigation.snapshot_id
    )
    patch = service.create_patch(
        db,
        investigation=investigation,
        snapshot=snapshot,
        instruction=body.instruction,
        edits=body.edits,
        answer_model=answer_model,
        embedder=embedder,
        settings=settings,
    )
    return _out(patch, snapshot.commit_sha)


@router.get("/investigations/{investigation_id}/patches", response_model=PatchList)
def list_patches(investigation_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> PatchList:
    investigation = _owned_investigation(db, workspace_id, investigation_id)
    rows = db.scalars(
        select(Patch)
        .where(Patch.investigation_id == investigation.id)
        .order_by(Patch.created_at.desc(), Patch.id.desc())
        .limit(100)
    )
    return PatchList(
        items=[
            PatchSummary.model_validate(
                {
                    "id": p.id,
                    "instruction": p.instruction,
                    "source": p.source,
                    "status": p.status,
                    "files": p.files,
                    "created_at": p.created_at,
                }
            )
            for p in rows
        ]
    )


@router.get("/patches/{patch_id}", response_model=PatchOut)
def get_patch(patch_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> PatchOut:
    patch = _owned_patch(db, workspace_id, patch_id)
    return _out(patch, _commit_sha(db, patch))


@router.post("/patches/{patch_id}/approve", response_model=PatchOut)
def approve_patch(
    patch_id: uuid.UUID, body: PatchApproval, db: Db, workspace_id: WorkspaceId
) -> PatchOut:
    """Approve the exact diff you reviewed, named by its SHA-256. Approval applies nothing."""
    patch = service.approve_patch(db, _owned_patch(db, workspace_id, patch_id), body.diff_sha256)
    return _out(patch, _commit_sha(db, patch))


@router.post("/patches/{patch_id}/reject", response_model=PatchOut)
def reject_patch(
    patch_id: uuid.UUID, body: PatchRejection, db: Db, workspace_id: WorkspaceId
) -> PatchOut:
    patch = service.reject_patch(db, _owned_patch(db, workspace_id, patch_id), body.reason)
    return _out(patch, _commit_sha(db, patch))


@router.post("/patches/{patch_id}/export", response_model=PatchExport)
def export_patch(patch_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> PatchExport:
    """The approved diff, ready for `git apply` on a checkout of the snapshot's commit."""
    patch = service.export_patch(db, _owned_patch(db, workspace_id, patch_id))
    assert patch.diff is not None and patch.diff_sha256 is not None
    commit_sha = _commit_sha(db, patch)
    return PatchExport(
        patch_id=patch.id,
        filename=f"codemind-{str(patch.id)[:8]}-{commit_sha[:7]}.diff",
        diff=patch.diff,
        diff_sha256=patch.diff_sha256,
        commit_sha=commit_sha,
    )


@router.post(
    "/patches/{patch_id}/test-runs",
    response_model=TestRunOut,
    status_code=status.HTTP_202_ACCEPTED,
    dependencies=[Depends(rate_limit("sandbox"))],
)
def create_test_run(
    patch_id: uuid.UUID,
    body: TestRunRequest,
    db: Db,
    workspace_id: WorkspaceId,
    enqueue: EnqueueTestRun,
) -> TestRunOut:
    """Run a fixed test profile on a throwaway patched copy of the snapshot, in a sandbox.

    Returns at once with a queued run; poll `GET /test-runs/{id}`. When running tests is turned off
    the run is recorded as `blocked`, never as passed.
    """
    patch = _owned_patch(db, workspace_id, patch_id)
    run = runs.create_test_run(db, patch, body.profile, settings, enqueue)
    return TestRunOut.model_validate(run, from_attributes=True)


@router.get("/patches/{patch_id}/test-runs", response_model=TestRunList)
def list_test_runs(patch_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> TestRunList:
    patch = _owned_patch(db, workspace_id, patch_id)
    rows = list(
        db.scalars(
            select(TestRun)
            .where(TestRun.patch_id == patch.id)
            .order_by(TestRun.created_at.desc(), TestRun.id.desc())
            .limit(50)
        )
    )
    for run in rows:
        runs.mark_lost_runs(db, run, settings)
    return TestRunList(items=[TestRunOut.model_validate(r, from_attributes=True) for r in rows])


@router.get("/test-runs/{test_run_id}", response_model=TestRunOut)
def get_test_run(test_run_id: uuid.UUID, db: Db, workspace_id: WorkspaceId) -> TestRunOut:
    run = db.get(TestRun, test_run_id)
    if run is None or run.workspace_id != workspace_id:
        raise NotFoundError("test_run_not_found", "No test run with that id.")
    runs.mark_lost_runs(db, run, settings)
    return TestRunOut.model_validate(run, from_attributes=True)


def _owned_investigation(
    db: Session, workspace_id: uuid.UUID, investigation_id: uuid.UUID
) -> Investigation:
    investigation = db.get(Investigation, investigation_id)
    if investigation is None or investigation.workspace_id != workspace_id:
        raise NotFoundError("investigation_not_found", "No investigation with that id.")
    return investigation


def _owned_patch(db: Session, workspace_id: uuid.UUID, patch_id: uuid.UUID) -> Patch:
    patch = db.get(Patch, patch_id)
    if patch is None or patch.workspace_id != workspace_id:
        raise NotFoundError("patch_not_found", "No patch with that id.")
    return patch


def _commit_sha(db: Session, patch: Patch) -> str:
    sha = db.scalar(
        select(RepositorySnapshot.commit_sha).where(RepositorySnapshot.id == patch.snapshot_id)
    )
    return sha or ""


def _out(patch: Patch, commit_sha: str) -> PatchOut:
    return PatchOut.model_validate(
        {
            "id": patch.id,
            "investigation_id": patch.investigation_id,
            "repository_id": patch.repository_id,
            "snapshot_id": patch.snapshot_id,
            "commit_sha": commit_sha,
            "instruction": patch.instruction,
            "source": patch.source,
            "status": patch.status,
            "summary": patch.summary,
            "limitations": patch.limitations,
            "edits": patch.edits,
            "files": patch.files,
            "diff": patch.diff,
            "diff_sha256": patch.diff_sha256,
            "validation": patch.validation,
            "error_code": patch.error_code,
            "error_message": patch.error_message,
            "metadata": patch.metadata_,
            "created_at": patch.created_at,
            "decided_at": patch.decided_at,
            "decided_by": patch.decided_by,
            "exported_at": patch.exported_at,
        }
    )
