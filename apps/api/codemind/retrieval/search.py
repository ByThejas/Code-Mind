"""Hybrid retrieval over one snapshot: keyword, symbol, path and vector candidates, fused and trimmed.

The best few candidates are then expanded along the code graph (what they call, what calls them, what
they extend), so a flow question also gets the code on the other side of each call.

Every result carries `retrieved_by` so evaluation and debugging can see why it was chosen (blueprint §10.3).
"""

import re
import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import Integer, exists, func, or_, select, text
from sqlalchemy.orm import Session

from codemind.core.errors import CodemindError
from codemind.db.models import Chunk, Embedding, File, GraphEdge, Symbol
from codemind.providers import EmbeddingProvider
from codemind.retrieval.query import ParsedQuery, parse_query

KEYWORD_LIMIT = 40
SYMBOL_LIMIT = 20
PATH_LIMIT = 10
VECTOR_LIMIT = 40
RRF_K = 60
WEIGHTS = {"keyword": 1.0, "vector": 1.0, "symbol": 1.5, "path": 1.0}
GRAPH_SEEDS = 5  # top candidates whose graph neighbours are added
# Share of a score that a callee or base class inherits per hop. Flow questions ("what happens
# when...") keep a call chain together; other questions only nudge neighbours up.
GRAPH_SHARE = 0.7
GRAPH_FLOW_SHARE = 0.95
GRAPH_CALLER_FACTOR = 0.7  # callers matter less than callees for explaining a symbol
FLOW_PATTERN = re.compile(
    r"\b(what happens|flow|trace|tracing|calls?|called|calling|reach(es)?|end(s)? up|goes|go "
    r"through|path|sequence|step|steps|pipeline|lifecycle|how (does|do|is|are))\b",
    re.IGNORECASE,
)
GRAPH_LIMIT = 15
GRAPH_MIN_CONFIDENCE = 0.6  # leave out name-only guesses
GRAPH_RELATIONS = ("calls", "extends", "implements")
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


@dataclass(frozen=True)
class EvidenceRelation:
    """A graph edge between two pieces of chosen evidence, shown to the answer model as a hint."""

    source_id: str  # evidence id
    source_symbol: str
    relation: str
    target_id: str
    target_symbol: str
    path: str
    line: int
    resolution: str

    def describe(self) -> str:
        return (
            f"{self.source_id} {self.source_symbol} {self.relation} {self.target_id} "
            f"{self.target_symbol} ({self.path} line {self.line}, {self.resolution})"
        )


@dataclass
class Retrieval:
    query: ParsedQuery
    evidence: list[Evidence]
    candidate_counts: dict[str, int]
    vector_search: str  # "used" | "no_embeddings" | "not_configured" | "failed"
    relations: list[EvidenceRelation] = field(default_factory=list)


def retrieve(
    db: Session,
    snapshot_id: uuid.UUID,
    question: str,
    *,
    embedder: EmbeddingProvider | None,
    token_budget: int,
    max_items: int,
    use_graph: bool = True,
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

    def fuse(source: str, ids: list[uuid.UUID]) -> None:
        for rank, chunk_id in enumerate(ids):
            scores[chunk_id] = scores.get(chunk_id, 0.0) + WEIGHTS[source] / (RRF_K + rank + 1)
            sources.setdefault(chunk_id, []).append(source)

    for source, ids in ranked.items():
        fuse(source, ids)
    if use_graph and scores:
        seeds = sorted(scores, key=lambda cid: scores[cid], reverse=True)[:GRAPH_SEEDS]
        share = GRAPH_FLOW_SHARE if is_flow_question(question) else GRAPH_SHARE
        inherited = _graph(db, snapshot_id, {cid: scores[cid] for cid in seeds}, share)
        ranked["graph"] = list(inherited)
        for chunk_id, score in inherited.items():
            # Raise, never add: a neighbour already found on its own keeps its rank if better.
            if score > scores.get(chunk_id, 0.0):
                scores[chunk_id] = score
                sources.setdefault(chunk_id, []).append("graph")

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
        relations=evidence_relations(db, snapshot_id, evidence) if use_graph else [],
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


def is_flow_question(question: str) -> bool:
    """Questions about what happens across code, where callees matter as much as the hit itself."""
    return FLOW_PATTERN.search(question) is not None


def _graph(
    db: Session, snapshot_id: uuid.UUID, seeds: dict[uuid.UUID, float], share: float
) -> dict[uuid.UUID, float]:
    """Chunks holding graph neighbours of the seed chunks' symbols, each with an inherited score.

    Two hops along outgoing edges (what the code calls and builds on) and one hop back to callers.
    A neighbour inherits `share` x edge confidence of the score that reached it, so it ranks just
    behind the code that led to it and never above it. Seeds can be raised the same way when a
    better seed calls them.
    """
    seed_rows = db.execute(
        select(Chunk.id, Symbol.id)
        .join(Symbol, Symbol.file_id == Chunk.file_id)
        .where(
            Chunk.id.in_(list(seeds)),
            Symbol.start_line >= Chunk.start_line,
            Symbol.start_line <= Chunk.end_line,
        )
    ).all()
    seed_score: dict[uuid.UUID, float] = {}
    for chunk_id, symbol_id in seed_rows:
        seed_score[symbol_id] = max(seed_score.get(symbol_id, 0.0), seeds[chunk_id])
    if not seed_score:
        return {}

    def hop(
        sources: dict[uuid.UUID, float], outgoing: bool, factor: float
    ) -> dict[uuid.UUID, float]:
        if not sources:
            return {}
        near = GraphEdge.source_symbol_id if outgoing else GraphEdge.target_symbol_id
        far = GraphEdge.target_symbol_id if outgoing else GraphEdge.source_symbol_id
        relations = GRAPH_RELATIONS if outgoing else ("calls",)
        rows = db.execute(
            select(near, far, GraphEdge.confidence).where(
                GraphEdge.snapshot_id == snapshot_id,
                near.in_(list(sources)),
                far.is_not(None),
                GraphEdge.relation.in_(relations),
                GraphEdge.confidence >= GRAPH_MIN_CONFIDENCE,
            )
        ).all()
        reached: dict[uuid.UUID, float] = {}
        for origin, other, confidence in rows:
            if origin is None or other is None:
                continue
            score = sources[origin] * factor * confidence
            if score > max(reached.get(other, 0.0), seed_score.get(other, 0.0)):
                reached[other] = score
        return reached

    first = hop(seed_score, outgoing=True, factor=share)
    second = hop(seed_score | first, outgoing=True, factor=share)
    callers = hop(seed_score, outgoing=False, factor=share * GRAPH_CALLER_FACTOR)
    inherited: dict[uuid.UUID, float] = {}
    for reached in (first, second, callers):
        for symbol_id, score in reached.items():
            inherited[symbol_id] = max(inherited.get(symbol_id, 0.0), score)
    if not inherited:
        return {}
    rows = db.execute(
        select(Symbol.id, Chunk.id)
        .join(Chunk, Chunk.file_id == Symbol.file_id)
        .where(
            Symbol.id.in_(list(inherited)),
            Chunk.start_line <= Symbol.start_line,
            Chunk.end_line >= Symbol.start_line,
        )
        .order_by(Chunk.end_line - Chunk.start_line, Chunk.ordinal)
    ).all()
    chunk_of: dict[uuid.UUID, uuid.UUID] = {}
    for symbol_id, chunk_id in rows:
        chunk_of.setdefault(symbol_id, chunk_id)  # the tightest chunk around the symbol
    result: dict[uuid.UUID, float] = {}
    for symbol_id, score in inherited.items():
        holder = chunk_of.get(symbol_id)
        if holder is not None:
            result[holder] = max(result.get(holder, 0.0), score)
    best = sorted(result, key=lambda cid: result[cid], reverse=True)[:GRAPH_LIMIT]
    return {cid: result[cid] for cid in best}


def evidence_relations(
    db: Session, snapshot_id: uuid.UUID, evidence: list[Evidence]
) -> list[EvidenceRelation]:
    """Graph edges whose two ends both fall inside chosen evidence (in different items)."""
    if not evidence:
        return []
    by_chunk = {item.chunk_id: item for item in evidence}
    rows = db.execute(
        select(Chunk.id, Symbol.id, Symbol.qualified_name)
        .join(Symbol, Symbol.file_id == Chunk.file_id)
        .where(
            Chunk.id.in_(list(by_chunk)),
            Symbol.start_line >= Chunk.start_line,
            Symbol.start_line <= Chunk.end_line,
        )
    ).all()
    holder: dict[uuid.UUID, tuple[Evidence, str]] = {}
    for chunk_id, symbol_id, name in rows:
        holder.setdefault(symbol_id, (by_chunk[chunk_id], name))
    if not holder:
        return []
    edges = db.execute(
        select(GraphEdge, File.path)
        .join(File, File.id == GraphEdge.source_file_id)
        .where(
            GraphEdge.snapshot_id == snapshot_id,
            GraphEdge.source_symbol_id.in_(list(holder)),
            GraphEdge.target_symbol_id.in_(list(holder)),
            GraphEdge.relation.in_(GRAPH_RELATIONS),
            GraphEdge.confidence >= GRAPH_MIN_CONFIDENCE,
        )
        .order_by(File.path, GraphEdge.line)
    ).all()
    relations = []
    for edge, path in edges:
        assert edge.source_symbol_id is not None and edge.target_symbol_id is not None
        (source, source_name), (target, target_name) = (
            holder[edge.source_symbol_id],
            holder[edge.target_symbol_id],
        )
        if source.id == target.id:
            continue
        relations.append(
            EvidenceRelation(
                source.id,
                source_name,
                edge.relation,
                target.id,
                target_name,
                path,
                edge.line,
                edge.resolution,
            )
        )
    return relations


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
