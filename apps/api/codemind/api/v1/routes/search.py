import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_current_workspace, get_db, get_embedding_provider
from codemind.api.v1.routes.files import servable_snapshot
from codemind.core.config import settings
from codemind.core.errors import CodemindError
from codemind.providers import EmbeddingProvider
from codemind.retrieval.search import retrieve
from codemind.schemas.retrieval import EvidenceOut, SearchResponse

router = APIRouter(prefix="/repositories/{repository_id}/search", tags=["Search"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]
Embedder = Annotated[EmbeddingProvider | None, Depends(get_embedding_provider)]


@router.get("", response_model=SearchResponse)
def search(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    embedder: Embedder,
    q: Annotated[str, Query(min_length=2, max_length=2000)],
    snapshot_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=50)] = 10,
) -> SearchResponse:
    """Hybrid retrieval only, with no answer model: the evidence a question would be answered from."""
    q = q.strip()
    if len(q) < 2:
        raise CodemindError(
            "invalid_query", "The query must have at least 2 characters.", status_code=422
        )
    snapshot = servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    result = retrieve(
        db,
        snapshot.id,
        q,
        embedder=embedder,
        token_budget=settings.answer_context_tokens,
        max_items=limit,
    )
    return SearchResponse(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        items=[EvidenceOut.model_validate(e.to_dict()) for e in result.evidence],
    )
