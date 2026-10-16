import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import select, tuple_
from sqlalchemy.orm import Session

from codemind.api.dependencies import (
    get_answer_model,
    get_current_workspace,
    get_db,
    get_embedding_provider,
)
from codemind.api.v1.routes.files import servable_snapshot
from codemind.api.v1.routes.repositories import get_owned_repository
from codemind.core.config import settings
from codemind.core.errors import CodemindError, NotFoundError
from codemind.db.models import Investigation, RepositorySnapshot
from codemind.providers import AnswerModel, EmbeddingProvider
from codemind.retrieval.answer import run_investigation
from codemind.schemas.retrieval import (
    InvestigationList,
    InvestigationOut,
    InvestigationRequest,
    InvestigationSummary,
)

router = APIRouter(tags=["Investigations"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]
Embedder = Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)]
Model = Annotated[AnswerModel | None, Depends(get_answer_model)]


@router.post(
    "/repositories/{repository_id}/investigations",
    response_model=InvestigationOut,
    status_code=status.HTTP_201_CREATED,
)
def create_investigation(
    repository_id: uuid.UUID,
    body: InvestigationRequest,
    db: Db,
    workspace_id: WorkspaceId,
    embedder: Embedder,
    answer_model: Model,
) -> InvestigationOut:
    """Answer a question about the repository with validated file and line citations."""
    snapshot = servable_snapshot(db, workspace_id, repository_id, body.snapshot_id)
    if answer_model is None:
        raise CodemindError(
            "answer_model_not_configured",
            "No answer model is configured. Set LLM_PROVIDER=ollama, or use search instead.",
            status_code=503,
        )
    investigation = run_investigation(
        db,
        workspace_id=workspace_id,
        snapshot=snapshot,
        question=body.question,
        embedder=embedder,
        answer_model=answer_model,
        settings=settings,
    )
    return _out(investigation, snapshot.commit_sha)


@router.get("/repositories/{repository_id}/investigations", response_model=InvestigationList)
def list_investigations(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    cursor: Annotated[uuid.UUID | None, Query(description="Last investigation id seen.")] = None,
    limit: Annotated[int, Query(ge=1, le=100)] = 20,
) -> InvestigationList:
    repo = get_owned_repository(db, workspace_id, repository_id)
    query = select(Investigation).where(Investigation.repository_id == repo.id)
    if cursor is not None:
        last = db.get(Investigation, cursor)
        if last is None or last.repository_id != repo.id:
            raise CodemindError("invalid_cursor", "Unknown cursor.", status_code=422)
        query = query.where(
            tuple_(Investigation.created_at, Investigation.id) < (last.created_at, last.id)
        )
    rows = list(
        db.scalars(
            query.order_by(Investigation.created_at.desc(), Investigation.id.desc()).limit(
                limit + 1
            )
        )
    )
    page = rows[:limit]
    return InvestigationList(
        items=[InvestigationSummary.model_validate(i) for i in page],
        next_cursor=str(page[-1].id) if len(rows) > limit else None,
    )


@router.get("/investigations/{investigation_id}", response_model=InvestigationOut)
def get_investigation(
    investigation_id: uuid.UUID, db: Db, workspace_id: WorkspaceId
) -> InvestigationOut:
    investigation = db.get(Investigation, investigation_id)
    if investigation is None or investigation.workspace_id != workspace_id:
        raise NotFoundError("investigation_not_found", "No investigation with that id.")
    commit_sha = db.scalar(
        select(RepositorySnapshot.commit_sha).where(
            RepositorySnapshot.id == investigation.snapshot_id
        )
    )
    return _out(investigation, commit_sha or "")


def _out(investigation: Investigation, commit_sha: str) -> InvestigationOut:
    return InvestigationOut.model_validate(
        {
            "id": investigation.id,
            "repository_id": investigation.repository_id,
            "snapshot_id": investigation.snapshot_id,
            "commit_sha": commit_sha,
            "question": investigation.question,
            "status": investigation.status,
            "answer": investigation.answer,
            "evidence": investigation.evidence,
            "validation": investigation.validation,
            "error_code": investigation.error_code,
            "error_message": investigation.error_message,
            "metadata": investigation.metadata_,
            "created_at": investigation.created_at,
            "finished_at": investigation.finished_at,
        }
    )
