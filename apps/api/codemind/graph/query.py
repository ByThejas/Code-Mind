"""Read the knowledge graph of one snapshot: symbol lookup, neighbourhoods, flow traces, modules."""

import posixpath
import uuid
from collections import Counter, defaultdict, deque
from collections.abc import Iterable, Sequence
from typing import Any

from sqlalchemy import Integer, and_, func, or_, select
from sqlalchemy.orm import Session

from codemind.core.errors import CodemindError, NotFoundError
from codemind.db.models import File, FileContent, GraphEdge, RepositorySnapshot, Symbol
from codemind.schemas.graph import (
    GraphEdgeOut,
    GraphInfo,
    GraphNode,
    SymbolOut,
    TraceStep,
)

SYMBOL_RELATIONS = ("calls", "extends", "implements")
FILE_RELATIONS = ("imports",)
MAX_TRACE_VISITS = 5000
MAX_SOURCE_LINE = 300

TRACE_LIMITATIONS = [
    (
        "Only relationships found by static analysis are followed. Calls through dynamic "
        "dispatch, callbacks, dependency injection, decorators or configuration are not in the graph."
    ),
]


def graph_info(snapshot: RepositorySnapshot) -> GraphInfo:
    stats = (snapshot.stats or {}).get("graph")
    warnings = [w for w in snapshot.warnings or [] if str(w.get("code", "")).startswith("graph_")]
    if stats is None:
        warnings.insert(
            0,
            {
                "code": "graph_not_built",
                "message": "This snapshot was indexed before the code graph existed. Re-index it "
                "with force to build the graph.",
            },
        )
    return GraphInfo(available=stats is not None, stats=stats or {}, warnings=warnings)


# --- symbols ---------------------------------------------------------------------------------


def search_symbols(
    db: Session,
    snapshot_id: uuid.UUID,
    *,
    q: str | None,
    kind: str | None,
    path_prefix: str | None,
    limit: int,
) -> list[SymbolOut]:
    stmt = (
        select(Symbol, File.path)
        .join(File, File.id == Symbol.file_id)
        .where(Symbol.snapshot_id == snapshot_id)
    )
    order: list[Any] = []
    if q:
        pattern = f"%{_escape_like(q)}%"
        stmt = stmt.where(
            or_(
                Symbol.name.ilike(pattern, escape="\\"),
                Symbol.qualified_name.ilike(pattern, escape="\\"),
            )
        )
        exact = or_(
            func.lower(Symbol.name) == q.lower(), func.lower(Symbol.qualified_name) == q.lower()
        )
        prefix = Symbol.name.ilike(f"{_escape_like(q)}%", escape="\\")
        order += [exact.cast(Integer).desc(), prefix.cast(Integer).desc(), func.length(Symbol.name)]
    if kind:
        stmt = stmt.where(Symbol.kind == kind)
    if path_prefix:
        stmt = stmt.where(File.path.like(f"{_escape_like(path_prefix)}%", escape="\\"))
    rows = db.execute(
        stmt.order_by(*order, File.path, Symbol.start_line, Symbol.id).limit(limit)
    ).all()
    return [symbol_out(symbol, path) for symbol, path in rows]


def get_symbol(db: Session, snapshot_id: uuid.UUID, symbol_id: uuid.UUID) -> tuple[Symbol, str]:
    row = db.execute(
        select(Symbol, File.path)
        .join(File, File.id == Symbol.file_id)
        .where(Symbol.id == symbol_id, Symbol.snapshot_id == snapshot_id)
    ).first()
    if row is None:
        raise NotFoundError("symbol_not_found", "No symbol with that id in this snapshot.")
    return row[0], row[1]


def symbol_family(
    db: Session, snapshot_id: uuid.UUID, symbol: Symbol
) -> tuple[SymbolOut | None, list[SymbolOut]]:
    parent = None
    if symbol.parent_symbol_id is not None:
        parent_symbol, path = get_symbol(db, snapshot_id, symbol.parent_symbol_id)
        parent = symbol_out(parent_symbol, path)
    rows = db.execute(
        select(Symbol, File.path)
        .join(File, File.id == Symbol.file_id)
        .where(Symbol.parent_symbol_id == symbol.id)
        .order_by(Symbol.start_line)
    ).all()
    return parent, [symbol_out(s, p) for s, p in rows]


def symbol_out(symbol: Symbol, path: str) -> SymbolOut:
    return SymbolOut(
        id=symbol.id,
        name=symbol.name,
        qualified_name=symbol.qualified_name,
        kind=symbol.kind,
        path=path,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
    )


# --- edges and nodes -------------------------------------------------------------------------


def symbol_node_id(symbol_id: uuid.UUID) -> str:
    return f"symbol:{symbol_id}"


def file_node_id(file_id: uuid.UUID) -> str:
    return f"file:{file_id}"


def _source_node(edge: GraphEdge) -> str:
    if edge.source_symbol_id is not None:
        return symbol_node_id(edge.source_symbol_id)
    return file_node_id(edge.source_file_id)


def _target_node(edge: GraphEdge) -> str:
    if edge.target_symbol_id is not None:
        return symbol_node_id(edge.target_symbol_id)
    return file_node_id(edge.target_file_id)


class NodeLoader:
    """Collects node ids while edges are gathered, then loads them in two queries."""

    def __init__(self, db: Session, snapshot_id: uuid.UUID) -> None:
        self.db = db
        self.snapshot_id = snapshot_id
        self.paths: dict[uuid.UUID, str] = {}

    def edge_out(self, edge: GraphEdge) -> GraphEdgeOut:
        return GraphEdgeOut(
            id=str(edge.id),
            source=_source_node(edge),
            target=_target_node(edge),
            relation=edge.relation,
            resolution=edge.resolution,  # type: ignore[arg-type]
            confidence=edge.confidence,
            provenance=edge.provenance,
            path=self.file_paths([edge.source_file_id])[edge.source_file_id],
            line=edge.line,
        )

    def file_paths(self, file_ids: Iterable[uuid.UUID]) -> dict[uuid.UUID, str]:
        missing = {f for f in file_ids if f not in self.paths}
        if missing:
            rows = self.db.execute(select(File.id, File.path).where(File.id.in_(missing))).all()
            self.paths.update({fid: path for fid, path in rows})
        return self.paths

    def preload_paths(self, edges: Sequence[GraphEdge]) -> None:
        self.file_paths({e.source_file_id for e in edges} | {e.target_file_id for e in edges})

    def nodes(self, node_ids: Iterable[str]) -> dict[str, GraphNode]:
        symbol_ids: set[uuid.UUID] = set()
        file_ids: set[uuid.UUID] = set()
        for node_id in node_ids:
            kind, _, raw = node_id.partition(":")
            (symbol_ids if kind == "symbol" else file_ids).add(uuid.UUID(raw))
        out: dict[str, GraphNode] = {}
        if symbol_ids:
            rows = self.db.execute(
                select(Symbol, File.path)
                .join(File, File.id == Symbol.file_id)
                .where(Symbol.id.in_(symbol_ids))
            ).all()
            for symbol, path in rows:
                out[symbol_node_id(symbol.id)] = symbol_node(symbol, path)
        if file_ids:
            file_rows = self.db.execute(
                select(File.id, File.path, File.language).where(File.id.in_(file_ids))
            ).all()
            for file_id, path, language in file_rows:
                out[file_node_id(file_id)] = file_node(file_id, path, language)
        return out


def symbol_node(symbol: Symbol, path: str) -> GraphNode:
    return GraphNode(
        id=symbol_node_id(symbol.id),
        type="symbol",
        label=symbol.name,
        path=path,
        kind=symbol.kind,
        qualified_name=symbol.qualified_name,
        start_line=symbol.start_line,
        end_line=symbol.end_line,
    )


def file_node(file_id: uuid.UUID, path: str, language: str | None) -> GraphNode:
    return GraphNode(
        id=file_node_id(file_id),
        type="file",
        label=posixpath.basename(path),
        path=path,
        kind=language,
    )


def symbol_edges(
    db: Session,
    snapshot_id: uuid.UUID,
    symbol_ids: Sequence[uuid.UUID],
    *,
    direction: str,
    relations: Sequence[str],
    min_confidence: float,
    limit: int,
) -> list[GraphEdge]:
    """Edges leaving ("out") or entering ("in") the given symbols, best confidence first."""
    column = GraphEdge.source_symbol_id if direction == "out" else GraphEdge.target_symbol_id
    stmt = (
        select(GraphEdge)
        .where(
            GraphEdge.snapshot_id == snapshot_id,
            column.in_(symbol_ids),
            GraphEdge.relation.in_(relations),
            GraphEdge.confidence >= min_confidence,
        )
        .order_by(GraphEdge.confidence.desc(), GraphEdge.line, GraphEdge.id)
        .limit(limit)
    )
    return list(db.scalars(stmt))


def file_edges(
    db: Session,
    snapshot_id: uuid.UUID,
    file_ids: Sequence[uuid.UUID],
    *,
    direction: str,
    min_confidence: float,
    limit: int,
) -> list[GraphEdge]:
    column = GraphEdge.source_file_id if direction == "out" else GraphEdge.target_file_id
    stmt = (
        select(GraphEdge)
        .where(
            GraphEdge.snapshot_id == snapshot_id,
            column.in_(file_ids),
            GraphEdge.relation.in_(FILE_RELATIONS),
            GraphEdge.confidence >= min_confidence,
        )
        .order_by(GraphEdge.confidence.desc(), GraphEdge.line, GraphEdge.id)
        .limit(limit)
    )
    return list(db.scalars(stmt))


# --- neighbourhood ---------------------------------------------------------------------------


def neighborhood(
    db: Session,
    snapshot_id: uuid.UUID,
    *,
    start: str,  # node id
    depth: int,
    direction: str,  # "in" | "out" | "both"
    relations: Sequence[str],
    min_confidence: float,
    max_nodes: int,
) -> tuple[list[GraphNode], list[GraphEdgeOut], bool]:
    """Breadth-first from one node with cycle protection; stops early at max_nodes."""
    kind, _, raw = start.partition(":")
    directions = ("out", "in") if direction == "both" else (direction,)
    seen = {start}
    frontier = [uuid.UUID(raw)]
    edges: dict[uuid.UUID, GraphEdge] = {}
    truncated = False
    for _ in range(depth):
        following: list[uuid.UUID] = []
        for way in directions:
            remaining = max_nodes * 4
            if kind == "symbol":
                found = symbol_edges(
                    db,
                    snapshot_id,
                    frontier,
                    direction=way,
                    relations=relations,
                    min_confidence=min_confidence,
                    limit=remaining,
                )
            else:
                found = file_edges(
                    db,
                    snapshot_id,
                    frontier,
                    direction=way,
                    min_confidence=min_confidence,
                    limit=remaining,
                )
            truncated |= len(found) >= remaining
            for edge in found:
                other = _target_node(edge) if way == "out" else _source_node(edge)
                if other not in seen:
                    if len(seen) >= max_nodes:
                        truncated = True
                        continue
                    seen.add(other)
                    other_kind, _, other_raw = other.partition(":")
                    if other_kind == kind:  # module-level callers are shown, not expanded
                        following.append(uuid.UUID(other_raw))
                edges[edge.id] = edge
        if not following:
            break
        frontier = following
    loader = NodeLoader(db, snapshot_id)
    ordered = sorted(edges.values(), key=lambda e: (-e.confidence, e.line, str(e.id)))
    loader.preload_paths(ordered)
    nodes = loader.nodes(seen)
    out_edges = [
        loader.edge_out(e) for e in ordered if _source_node(e) in seen and _target_node(e) in seen
    ]
    order = [start] + sorted(n for n in nodes if n != start)
    return [nodes[n] for n in order if n in nodes], out_edges, truncated


# --- flow trace ------------------------------------------------------------------------------


def trace(
    db: Session,
    snapshot_id: uuid.UUID,
    *,
    source: uuid.UUID,
    target: uuid.UUID,
    max_depth: int,
    relations: Sequence[str],
    min_confidence: float,
) -> tuple[list[TraceStep], int]:
    """Shortest path along outgoing edges from source to target. Empty steps when none exists."""
    parents: dict[uuid.UUID, GraphEdge | None] = {source: None}
    queue: deque[tuple[uuid.UUID, int]] = deque([(source, 0)])
    found = source == target
    while queue and not found and len(parents) < MAX_TRACE_VISITS:
        # Expand a whole depth level per query.
        level = queue[0][1]
        if level >= max_depth:
            break
        batch = []
        while queue and queue[0][1] == level:
            batch.append(queue.popleft()[0])
        for edge in symbol_edges(
            db,
            snapshot_id,
            batch,
            direction="out",
            relations=relations,
            min_confidence=min_confidence,
            limit=MAX_TRACE_VISITS,
        ):
            nxt = edge.target_symbol_id
            if nxt is None or nxt in parents:
                continue
            parents[nxt] = edge
            if nxt == target:
                found = True
                break
            queue.append((nxt, level + 1))
    if not found:
        return [], len(parents)
    chain: list[tuple[uuid.UUID, GraphEdge | None]] = []
    current: uuid.UUID | None = target
    while current is not None:
        via = parents[current]
        chain.append((current, via))
        current = via.source_symbol_id if via is not None else None
    chain.reverse()
    loader = NodeLoader(db, snapshot_id)
    edges = [via for _, via in chain if via is not None]
    loader.preload_paths(edges)
    nodes = loader.nodes(symbol_node_id(s) for s, _ in chain)
    lines = _source_lines(db, edges)
    steps = [
        TraceStep(
            node=nodes[symbol_node_id(symbol_id)],
            via=loader.edge_out(via) if via is not None else None,
            source_line=lines.get(via.id) if via is not None else None,
        )
        for symbol_id, via in chain
    ]
    return steps, len(parents)


def _source_lines(db: Session, edges: Sequence[GraphEdge]) -> dict[uuid.UUID, str]:
    file_ids = {e.source_file_id for e in edges}
    if not file_ids:
        return {}
    texts = dict(
        db.execute(
            select(FileContent.file_id, FileContent.text).where(FileContent.file_id.in_(file_ids))
        ).all()
    )
    out = {}
    for edge in edges:
        lines = texts.get(edge.source_file_id, "").splitlines()
        if 1 <= edge.line <= len(lines):
            out[edge.id] = lines[edge.line - 1].strip()[:MAX_SOURCE_LINE]
    return out


# --- modules ---------------------------------------------------------------------------------


def module_graph(
    db: Session, snapshot_id: uuid.UUID, *, depth: int, path_prefix: str | None
) -> tuple[list[GraphNode], list[GraphEdgeOut]]:
    """Import edges merged by directory: the repository's architecture at a chosen depth."""
    files = db.execute(
        select(File.id, File.path).where(
            File.snapshot_id == snapshot_id,
            File.excluded_reason.is_(None),
            File.language.is_not(None),
        )
    ).all()
    prefix = (path_prefix or "").strip("/")

    def group(path: str) -> str | None:
        if prefix and not (path == prefix or path.startswith(prefix + "/")):
            return None
        rest = path[len(prefix) :].lstrip("/") if prefix else path
        parts = rest.split("/")
        dirs = parts[:-1][:depth]
        return "/".join(([prefix] if prefix else []) + dirs) or (prefix or ".")

    group_of = {fid: g for fid, path in files if (g := group(path)) is not None}
    file_counts = Counter(group_of.values())
    source_file = File.__table__.alias("source_file")
    rows = db.execute(
        select(
            GraphEdge.source_file_id, GraphEdge.target_file_id, source_file.c.path, GraphEdge.line
        )
        .join(source_file, source_file.c.id == GraphEdge.source_file_id)
        .where(GraphEdge.snapshot_id == snapshot_id, GraphEdge.relation == "imports")
        .order_by(source_file.c.path, GraphEdge.line)
    ).all()
    merged: dict[tuple[str, str], list[tuple[str, int]]] = defaultdict(list)
    for source_id, target_id, path, line in rows:
        a, b = group_of.get(source_id), group_of.get(target_id)
        if a is not None and b is not None and a != b:
            merged[(a, b)].append((path, line))
    nodes = [
        GraphNode(
            id=f"module:{name}",
            type="module",
            label=posixpath.basename(name) or name,
            path=name,
            file_count=count,
        )
        for name, count in sorted(file_counts.items())
    ]
    edges = [
        GraphEdgeOut(
            id=f"module:{a}->{b}",
            source=f"module:{a}",
            target=f"module:{b}",
            relation="imports",
            resolution="static",
            confidence=1.0,
            provenance="aggregate:imports",
            path=sites[0][0],
            line=sites[0][1],
            count=len(sites),
        )
        for (a, b), sites in sorted(merged.items())
    ]
    return nodes, edges


def resolve_file(db: Session, snapshot_id: uuid.UUID, path: str) -> uuid.UUID:
    file_id = db.scalar(
        select(File.id).where(
            and_(File.snapshot_id == snapshot_id, File.path == path, File.excluded_reason.is_(None))
        )
    )
    if file_id is None:
        raise NotFoundError("file_not_found", "No indexed file with that path in this snapshot.")
    return file_id


def require_one(symbol_id: uuid.UUID | None, path: str | None) -> None:
    if (symbol_id is None) == (path is None):
        raise CodemindError(
            "invalid_graph_start", "Give exactly one of symbol_id or path.", status_code=422
        )


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
