import uuid
from typing import Literal

from pydantic import BaseModel, Field, model_validator

from codemind.schemas.graph import GraphEdgeOut, GraphInfo, GraphNode, SymbolOut


class ImpactTarget(BaseModel):
    """Something that will change: a symbol, a whole file, or some lines of a file."""

    symbol_id: uuid.UUID | None = None
    path: str | None = Field(default=None, max_length=4000)
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode="after")
    def _one_kind(self) -> "ImpactTarget":
        if (self.symbol_id is None) == (self.path is None):
            raise ValueError("give exactly one of symbol_id or path")
        if self.symbol_id is not None and (self.start_line or self.end_line):
            raise ValueError("lines only apply to a path")
        if self.start_line and self.end_line and self.end_line < self.start_line:
            raise ValueError("end_line is before start_line")
        return self


class ImpactRequest(BaseModel):
    targets: list[ImpactTarget] = Field(default_factory=list, max_length=100)
    diff: str | None = Field(
        default=None, max_length=1_000_000, description="A unified diff against the snapshot."
    )
    depth: int = Field(default=3, ge=1, le=6, description="How many dependency hops to follow.")
    min_confidence: float = Field(default=0.0, ge=0, le=1)
    max_results: int = Field(default=50, ge=1, le=300)
    snapshot_id: uuid.UUID | None = None

    @model_validator(mode="after")
    def _something(self) -> "ImpactRequest":
        if not self.targets and not (self.diff and self.diff.strip()):
            raise ValueError("give targets, a diff, or both")
        return self


class ChangedFile(BaseModel):
    path: str
    status: Literal["modified", "deleted", "renamed", "added", "target"]
    lines: list[
        list[int]
    ]  # changed line ranges [start, end] in the snapshot; [] for the whole file
    symbols: list[SymbolOut]  # symbols those lines fall in
    module_level: bool  # some changed lines are outside every symbol


class ImpactItem(BaseModel):
    """A symbol or file that depends, directly or through others, on the changed code."""

    node: GraphNode
    distance: int  # dependency hops from the nearest changed code
    confidence: float  # product of the edge confidences along the path
    score: float  # confidence discounted by distance; the ranking key
    via: GraphEdgeOut  # the edge from this item to the next step towards the change
    source_line: str | None  # the text of that edge's line
    changed: str  # node id of the changed symbol or file it was reached from
    path_to_change: list[str]  # node ids from this item to the change, both ends included


class ImpactFile(BaseModel):
    path: str
    score: float  # best score of anything impacted in the file
    distance: int
    items: int  # impacted symbols (or the file itself) in this file


class RelatedTest(BaseModel):
    path: str
    node: GraphNode | None  # the test function, when the graph reaches one
    reason: Literal["calls_changed_code", "imports_changed_code", "name_match", "mentions_symbol"]
    score: float
    distance: int | None  # graph hops, for reasons found through the graph
    detail: str


class Unresolved(BaseModel):
    path: str | None
    symbol_id: uuid.UUID | None = None
    reason: str


class ImpactResponse(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    advisory: bool = True  # static analysis cannot see every dependency; review the evidence
    changed: list[ChangedFile]
    impacted: list[ImpactItem]
    files: list[ImpactFile]
    tests: list[RelatedTest]
    unresolved: list[Unresolved]
    truncated: bool
    explored: int
    limitations: list[str]
    settings: dict[str, float | int]
    graph: GraphInfo
