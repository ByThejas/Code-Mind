"""The index job's embed stage: one vector per chunk, written in batches.

A provider failure never fails indexing. Batches already written stay, the snapshot gets an
`embeddings_incomplete` warning, and search falls back to keyword and symbol matches.
"""

import logging
import uuid
from collections.abc import Callable
from dataclasses import dataclass

from sqlalchemy import insert, select
from sqlalchemy.orm import Session

from codemind.core.errors import CodemindError
from codemind.db.models import Chunk, Embedding
from codemind.providers import EmbeddingProvider

log = logging.getLogger(__name__)


@dataclass
class EmbedResult:
    total: int
    embedded: int = 0
    error_code: str | None = None
    error_message: str | None = None


def embed_snapshot(
    db: Session,
    snapshot_id: uuid.UUID,
    provider: EmbeddingProvider,
    *,
    batch_size: int,
    after_batch: Callable[[int, int], None] = lambda done, total: None,
) -> EmbedResult:
    """Embed the snapshot's chunks that have no vector for this model yet. Commits per batch.

    `after_batch(done, total)` runs after each commit. A CodemindError from it (the job's time
    limit) stops embedding like a provider failure; anything else (cancellation) propagates.
    """
    missing = (
        select(Chunk.id, Chunk.text)
        .where(
            Chunk.snapshot_id == snapshot_id,
            ~select(Embedding.chunk_id)
            .where(Embedding.chunk_id == Chunk.id, Embedding.model_name == provider.model_name)
            .exists(),
        )
        .order_by(Chunk.file_id, Chunk.ordinal)
    )
    rows = db.execute(missing).all()
    result = EmbedResult(total=len(rows))
    for start in range(0, len(rows), batch_size):
        batch = rows[start : start + batch_size]
        try:
            vectors = provider.embed([row.text for row in batch], "document")
        except CodemindError as exc:
            return _stopped(db, result, exc)
        db.execute(
            insert(Embedding),
            [
                {
                    "chunk_id": row.id,
                    "model_name": provider.model_name,
                    "dimensions": provider.dimensions,
                    "embedding": vector,
                }
                for row, vector in zip(batch, vectors, strict=True)
            ],
        )
        db.commit()
        result.embedded += len(batch)
        try:
            after_batch(result.embedded, result.total)
        except CodemindError as exc:  # e.g. the job's time limit: keep what is embedded
            return _stopped(db, result, exc)
    return result


def _stopped(db: Session, result: EmbedResult, exc: CodemindError) -> EmbedResult:
    log.warning("embedding stopped after %s/%s chunks: %s", result.embedded, result.total, exc)
    db.rollback()
    result.error_code, result.error_message = exc.code, exc.message
    return result
