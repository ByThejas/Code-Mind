"""Hybrid retrieval over one snapshot: keyword, symbol, path and vector candidates, fused and trimmed.

Every result carries `retrieved_by` so evaluation and debugging can see why it was chosen (blueprint §10.3).
"""

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Integer, exists, func, or_, select, text
from sqlalchemy.orm import Session

from codemind.core.errors import CodemindError
from codemind.db.models import Chunk, Embedding, File, Symbol
from codemind.providers import EmbeddingProvider
from codemind.retrieval.query import ParsedQuery, parse_query

KEYWORD_LIMIT = 40
SYMBOL_LIMIT = 20
PATH_LIMIT = 10
VECTOR_LIMIT = 40
RRF_K = 60
WEIGHTS = {"keyword": 1.0, "vector": 1.0, "symbol": 1.5, "path": 1.0}
MAX_OVERLAP = 0.5
HNSW_EF_SEARCH = 200


@dataclass
class Evidence:
    id: str
    chunk_id: uuid.UUID
    path: str
    language: str | None
    start_line: int
    end_line: int
    symbol_name: str | None
    text: str
    token_estimate: int
    score: float = 0.0
    retrieved_by: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "chunk_id": str(self.chunk_id),
            "path": self.path,
            "language": self.language,
            "start_line": self.start_line,
            "end_line": self.end_line,
            "symbol_name": self.symbol_name,
            "retrieved_by": self.retrieved_by,
            "score": round(self.score, 6),
            "text": self.text,
        }


@dataclass
class Retrieval:
    query: ParsedQuery
    evidence: list[Evidence]
    candidate_counts: dict[str, int]
    vector_search: str  # "used" | "no_embeddings" | "not_configured" | "failed"


def retrieve(
    db: Session,
    snapshot_id: uuid.UUID,
    question: str,
    *,
    embedder: EmbeddingProvider | None,
    token_budget: int,
    max_items: int,
) -> Retrieval:
    query = parse_query(question)
    ranked: dict[str, list[uuid.UUID]] = {
        "keyword": _keyword(db, snapshot_id, query),
        "symbol": _symbols(db, snapshot_id, query.identifiers),
        "path": _paths(db, snapshot_id, query.paths),
    }
    vector_search = "not_configured"
    if embedder is not None:
        if _has_embeddings(db, snapshot_id, embedder.model_name):
            try:
                [vector] = embedder.embed([question], "query")
            except CodemindError:
                vector_search = "failed"  # keyword and symbol results still answer the question
            else:
                ranked["vector"] = _vector(db, snapshot_id, embedder.model_name, vector)
                vector_search = "used"
        else:
            vector_search = "no_embeddings"

    scores: dict[uuid.UUID, float] = {}
    sources: dict[uuid.UUID, list[str]] = {}
    for source, ids in ranked.items():
        for rank, chunk_id in enumerate(ids):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + WEIGHTS[source] / (RRF_K + rank + 1)
            sources.setdefault(chunk_id, []).append(source)

    ordered = sorted(scores, key=lambda cid: scores[cid], reverse=True)
    candidates = _load(db, ordered)
    for item in candidates:
        item.score, item.retrieved_by = scores[item.chunk_id], sources[item.chunk_id]
    evidence = assemble(dedupe(candidates), token_budget=token_budget, max_items=max_items)
    return Retrieval(
        query=query,
        evidence=evidence,
        candidate_counts={source: len(ids) for source, ids in ranked.items()},
        vector_search=vector_search,
    )


def dedupe(candidates: list[Evidence]) -> list[Evidence]:
    """Drop candidates mostly covered by a better-ranked one in the same file; keep their sources."""
    kept: list[Evidence] = []
    for item in candidates:
        twin = next(
            (k for k in kept if k.path == item.path and _overlap(k, item) > MAX_OVERLAP), None
        )
        if twin is None:
            kept.append(item)
        else:
            twin.retrieved_by.extend(s for s in item.retrieved_by if s not in twin.retrieved_by)
    return kept


def assemble(candidates: list[Evidence], *, token_budget: int, max_items: int) -> list[Evidence]:
    """Best first until the token budget or item cap is reached; ids are assigned in that order."""
    chosen: list[Evidence] = []
    used = 0
    for item in candidates:
        if len(chosen) >= max_items:
            break
        if chosen and used + item.token_estimate > token_budget:
            continue  # a smaller, lower-ranked chunk may still fit
        chosen.append(item)
        used += item.token_estimate
    for n, item in enumerate(chosen, start=1):
        item.id = f"ev_{n}"
    return chosen


def _overlap(a: Evidence, b: Evidence) -> float:
    shared = min(a.end_line, b.end_line) - max(a.start_line, b.start_line) + 1
    if shared <= 0:
        return 0.0
    return shared / min(a.end_line - a.start_line + 1, b.end_line - b.start_line + 1)


def _keyword(db: Session, snapshot_id: uuid.UUID, query: ParsedQuery) -> list[uuid.UUID]:
    if query.tsquery is None:
        return []
    tsquery = func.to_tsquery("simple", query.tsquery)
    rank = func.ts_rank_cd(Chunk.search_tsv, tsquery)
    stmt = (
        select(Chunk.id)
        .where(Chunk.snapshot_id == snapshot_id, Chunk.search_tsv.op("@@")(tsquery))
        .order_by(rank.desc(), Chunk.id)
        .limit(KEYWORD_LIMIT)
    )
    return list(db.scalars(stmt))


def _symbols(db: Session, snapshot_id: uuid.UUID, identifiers: list[str]) -> list[uuid.UUID]:
    """Chunks containing a symbol named like an identifier in the question (`greet`, `Store.get`)."""
    if not identifiers:
        return []
    conditions = []
    for ident in identifiers:
        conditions += [
            Symbol.qualified_name == ident,
            Symbol.name == ident,
            Symbol.qualified_name.like(f"%.{_escape_like(ident)}", escape="\\"),
        ]
    exact = Symbol.qualified_name.in_(identifiers)
    stmt = (
        select(Chunk.id)
        .join(Symbol, Symbol.file_id == Chunk.file_id)
        .where(
            Symbol.snapshot_id == snapshot_id,
            or_(*conditions),
            Chunk.start_line <= Symbol.start_line,
            Chunk.end_line >= Symbol.start_line,
        )
        .group_by(Chunk.id)
        .order_by(func.max(exact.cast(Integer)).desc(), func.min(Symbol.start_line), Chunk.id)
        .limit(SYMBOL_LIMIT)
    )
    return list(db.scalars(stmt))


def _paths(db: Session, snapshot_id: uuid.UUID, paths: list[str]) -> list[uuid.UUID]:
    if not paths:
        return []
    conditions = []
    for path in paths:
        conditions.append(File.path == path)
        conditions.append(File.path.like(f"%/{_escape_like(path)}", escape="\\"))
    stmt = (
        select(Chunk.id)
        .join(File, File.id == Chunk.file_id)
        .where(Chunk.snapshot_id == snapshot_id, or_(*conditions))
        .order_by(Chunk.ordinal, File.path)
        .limit(PATH_LIMIT)
    )
    return list(db.scalars(stmt))


def _has_embeddings(db: Session, snapshot_id: uuid.UUID, model_name: str) -> bool:
    stmt = select(
        exists().where(
            Embedding.chunk_id == Chunk.id,
            Chunk.snapshot_id == snapshot_id,
            Embedding.model_name == model_name,
        )
    )
    return bool(db.scalar(stmt))


def _vector(
    db: Session, snapshot_id: uuid.UUID, model_name: str, vector: list[float]
) -> list[uuid.UUID]:
    # The HNSW index spans all snapshots and filtering happens after the scan, so widen the
    # candidate list (docs/retrieval-design.md §4).
    db.execute(text(f"SET LOCAL hnsw.ef_search = {HNSW_EF_SEARCH}"))
    stmt = (
        select(Chunk.id)
        .join(Embedding, Embedding.chunk_id == Chunk.id)
        .where(Chunk.snapshot_id == snapshot_id, Embedding.model_name == model_name)
        .order_by(Embedding.embedding.cosine_distance(vector))
        .limit(VECTOR_LIMIT)
    )
    return list(db.scalars(stmt))


def _load(db: Session, ordered: Iterable[uuid.UUID]) -> list[Evidence]:
    ids = list(ordered)
    if not ids:
        return []
    rows = db.execute(
        select(Chunk, File.path, File.language)
        .join(File, File.id == Chunk.file_id)
        .where(Chunk.id.in_(ids))
    ).all()
    by_id = {
        chunk.id: Evidence(
            id="",
            chunk_id=chunk.id,
            path=path,
            language=language,
            start_line=chunk.start_line,
            end_line=chunk.end_line,
            symbol_name=chunk.symbol_name,
            text=chunk.text,
            token_estimate=chunk.token_estimate,
        )
        for chunk, path, language in rows
    }
    return [by_id[cid] for cid in ids if cid in by_id]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
