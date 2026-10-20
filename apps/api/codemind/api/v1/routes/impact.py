"""Impact analysis of a proposed change (docs/impact-design.md)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_current_workspace, get_db
from codemind.api.v1.routes.files import servable_snapshot
from codemind.impact.analysis import analyze
from codemind.schemas.impact import ImpactRequest, ImpactResponse

router = APIRouter(prefix="/repositories/{repository_id}", tags=["Impact"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]


@router.post("/impact-analysis", response_model=ImpactResponse)
def impact_analysis(
    repository_id: uuid.UUID, body: ImpactRequest, db: Db, workspace_id: WorkspaceId
) -> ImpactResponse:
    """What may be affected by changing some symbols, files, lines, or by applying a diff.

    Walks callers, subclasses and importers in the indexed snapshot, ranks them by edge confidence
    and distance, lists related tests, and says what static analysis could not see. Advisory only.
    """
    snapshot = servable_snapshot(db, workspace_id, repository_id, body.snapshot_id)
    return analyze(db, snapshot, body)
