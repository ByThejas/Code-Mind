import uuid
from typing import Any, Literal

from pydantic import BaseModel

NodeType = Literal["file", "symbol", "module"]


class GraphInfo(BaseModel):
    """Whether this snapshot has a graph, and what static analysis could not see."""

    available: bool  # False for snapshots indexed before the graph existed: re-index to build it
    stats: dict[str, Any]
    warnings: list[dict[str, Any]]


class GraphNode(BaseModel):
    id: str  # "symbol:<uuid>", "file:<uuid>" or "module:<path>"
    type: NodeType
    label: str
    path: str
    kind: str | None = None  # symbol kind, or the file's language
    qualified_name: str | None = None
    start_line: int | None = None
    end_line: int | None = None
    file_count: int | None = None  # module nodes only


class GraphEdgeOut(BaseModel):
    id: str
    source: str  # node id
    target: str
    relation: str
    resolution: Literal["static", "inferred"]
    confidence: float
    provenance: str
    path: str  # file where the source names the target
    line: int
    count: int = 1  # module edges: how many file imports were merged


class SymbolOut(BaseModel):
    id: uuid.UUID
    name: str
    qualified_name: str
    kind: str
    path: str
    start_line: int
    end_line: int


class SymbolList(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    items: list[SymbolOut]


class SymbolDetail(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    symbol: SymbolOut
    parent: SymbolOut | None
    children: list[SymbolOut]
    outgoing: list[GraphEdgeOut]  # what this symbol calls, extends or implements
    incoming: list[GraphEdgeOut]  # who calls, extends or implements it
    nodes: list[GraphNode]  # every node the edges mention
    truncated: bool
    graph: GraphInfo


class GraphResponse(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    nodes: list[GraphNode]
    edges: list[GraphEdgeOut]
    truncated: bool
    graph: GraphInfo


class TraceStep(BaseModel):
    node: GraphNode
    via: GraphEdgeOut | None  # the edge from the previous step; None for the first step
    source_line: str | None  # the text of the line where the previous step names this one


class TraceResponse(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    found: bool
    steps: list[TraceStep]
    explored: int  # symbols visited before giving up or finding the target
    limitations: list[str]
    graph: GraphInfo
