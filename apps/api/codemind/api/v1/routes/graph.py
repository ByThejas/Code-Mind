"""Symbols and the code graph of a repository's indexed snapshot (docs/graph-design.md)."""

import uuid
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from codemind.api.dependencies import get_current_workspace, get_db
from codemind.api.v1.routes.files import servable_snapshot
from codemind.core.errors import CodemindError
from codemind.graph import query as graph
from codemind.schemas.graph import GraphResponse, SymbolDetail, SymbolList, TraceResponse

router = APIRouter(prefix="/repositories/{repository_id}", tags=["Graph"])

Db = Annotated[Session, Depends(get_db)]
WorkspaceId = Annotated[uuid.UUID, Depends(get_current_workspace)]
Relation = Literal["calls", "extends", "implements"]
MinConfidence = Annotated[
    float,
    Query(ge=0, le=1, description="Drop inferred edges below this confidence (0.3 to 1.0)."),
]

MAX_DETAIL_EDGES = 200


@router.get("/symbols", response_model=SymbolList)
def list_symbols(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    q: Annotated[str | None, Query(max_length=200, description="Part of a name.")] = None,
    kind: Annotated[str | None, Query(max_length=20)] = None,
    path_prefix: Annotated[str | None, Query(max_length=1000)] = None,
    snapshot_id: uuid.UUID | None = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
) -> SymbolList:
    """Find functions, classes, methods and types by name; exact and prefix matches first."""
    snapshot = servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    items = graph.search_symbols(
        db, snapshot.id, q=q, kind=kind, path_prefix=path_prefix, limit=limit
    )
    return SymbolList(snapshot_id=snapshot.id, commit_sha=snapshot.commit_sha, items=items)


@router.get("/symbols/{symbol_id}", response_model=SymbolDetail)
def symbol_detail(
    repository_id: uuid.UUID,
    symbol_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    snapshot_id: uuid.UUID | None = None,
    min_confidence: MinConfidence = 0.0,
) -> SymbolDetail:
    """A symbol with what it calls, extends or implements, and who calls, extends or implements it."""
    snapshot = servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    symbol, path = graph.get_symbol(db, snapshot.id, symbol_id)
    parent, children = graph.symbol_family(db, snapshot.id, symbol)
    found = {
        way: graph.symbol_edges(
            db,
            snapshot.id,
            [symbol.id],
            direction=way,
            relations=graph.SYMBOL_RELATIONS,
            min_confidence=min_confidence,
            limit=MAX_DETAIL_EDGES + 1,
        )
        for way in ("out", "in")
    }
    truncated = any(len(edges) > MAX_DETAIL_EDGES for edges in found.values())
    loader = graph.NodeLoader(db, snapshot.id)
    kept = {way: edges[:MAX_DETAIL_EDGES] for way, edges in found.items()}
    loader.preload_paths(kept["out"] + kept["in"])
    outgoing = [loader.edge_out(e) for e in kept["out"]]
    incoming = [loader.edge_out(e) for e in kept["in"]]
    node_ids = {e.target for e in outgoing} | {e.source for e in incoming}
    node_ids.add(graph.symbol_node_id(symbol.id))
    nodes = loader.nodes(node_ids)
    return SymbolDetail(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        symbol=graph.symbol_out(symbol, path),
        parent=parent,
        children=children,
        outgoing=outgoing,
        incoming=incoming,
        nodes=sorted(nodes.values(), key=lambda n: (n.path, n.start_line or 0)),
        truncated=truncated,
        graph=graph.graph_info(snapshot),
    )


@router.get("/graph/neighborhood", response_model=GraphResponse)
def neighborhood(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    symbol_id: uuid.UUID | None = None,
    path: Annotated[
        str | None, Query(max_length=4000, description="A file: follows its imports instead.")
    ] = None,
    depth: Annotated[int, Query(ge=1, le=3)] = 1,
    direction: Literal["in", "out", "both"] = "both",
    relations: Annotated[list[Relation] | None, Query()] = None,
    min_confidence: MinConfidence = 0.0,
    max_nodes: Annotated[int, Query(ge=2, le=300)] = 100,
    snapshot_id: uuid.UUID | None = None,
) -> GraphResponse:
    """The graph around one symbol (calls, inheritance) or one file (imports)."""
    graph.require_one(symbol_id, path)
    snapshot = servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    if symbol_id is not None:
        graph.get_symbol(db, snapshot.id, symbol_id)
        start = graph.symbol_node_id(symbol_id)
    else:
        assert path is not None
        start = graph.file_node_id(graph.resolve_file(db, snapshot.id, path))
    nodes, edges, truncated = graph.neighborhood(
        db,
        snapshot.id,
        start=start,
        depth=depth,
        direction=direction,
        relations=relations or graph.SYMBOL_RELATIONS,
        min_confidence=min_confidence,
        max_nodes=max_nodes,
    )
    return GraphResponse(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        nodes=nodes,
        edges=edges,
        truncated=truncated,
        graph=graph.graph_info(snapshot),
    )


@router.get("/graph/trace", response_model=TraceResponse)
def trace(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    from_symbol_id: uuid.UUID,
    to_symbol_id: uuid.UUID,
    max_depth: Annotated[int, Query(ge=1, le=10)] = 6,
    relations: Annotated[list[Relation] | None, Query()] = None,
    min_confidence: MinConfidence = 0.0,
    snapshot_id: uuid.UUID | None = None,
) -> TraceResponse:
    """The shortest chain of calls from one symbol to another, with the calling line at each step."""
    snapshot = servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    info = graph.graph_info(snapshot)
    if not info.available:
        raise CodemindError(
            "graph_not_built",
            "This snapshot has no code graph yet. Re-index it with force to build one.",
            status_code=409,
        )
    graph.get_symbol(db, snapshot.id, from_symbol_id)
    graph.get_symbol(db, snapshot.id, to_symbol_id)
    steps, explored = graph.trace(
        db,
        snapshot.id,
        source=from_symbol_id,
        target=to_symbol_id,
        max_depth=max_depth,
        relations=relations or ("calls",),
        min_confidence=min_confidence,
    )
    limitations = list(graph.TRACE_LIMITATIONS)
    if not steps:
        limitations.insert(
            0,
            f"No chain of statically known calls within {max_depth} steps connects these symbols.",
        )
    elif any(s.via is not None and s.via.resolution == "inferred" for s in steps):
        limitations.insert(0, "Some steps are inferred from names, not proven; check their lines.")
    return TraceResponse(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        found=bool(steps),
        steps=steps,
        explored=explored,
        limitations=limitations,
        graph=info,
    )


@router.get("/graph/modules", response_model=GraphResponse)
def modules(
    repository_id: uuid.UUID,
    db: Db,
    workspace_id: WorkspaceId,
    depth: Annotated[int, Query(ge=1, le=6, description="Directory levels to keep.")] = 2,
    path_prefix: Annotated[str | None, Query(max_length=1000)] = None,
    snapshot_id: uuid.UUID | None = None,
) -> GraphResponse:
    """Imports merged by directory: which parts of the repository depend on which."""
    snapshot = servable_snapshot(db, workspace_id, repository_id, snapshot_id)
    nodes, edges = graph.module_graph(db, snapshot.id, depth=depth, path_prefix=path_prefix)
    return GraphResponse(
        snapshot_id=snapshot.id,
        commit_sha=snapshot.commit_sha,
        nodes=nodes,
        edges=edges,
        truncated=False,
        graph=graph.graph_info(snapshot),
    )
