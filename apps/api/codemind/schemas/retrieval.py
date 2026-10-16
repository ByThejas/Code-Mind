import uuid
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

from codemind.schemas.ingestion import ORMModel

# What the answer model must return (blueprint §10.4). It may cite only the evidence ids it was given.


class ClaimDraft(BaseModel):
    text: str = Field(max_length=2000)
    evidence_ids: list[str] = Field(max_length=20)
    status: Literal["supported", "hypothesis"]


class AnswerDraft(BaseModel):
    summary: str = Field(max_length=4000)
    claims: list[ClaimDraft] = Field(max_length=30)
    limitations: list[str] = Field(max_length=20)
    suggested_next_steps: list[str] = Field(max_length=20)


# What the API returns after the server has validated the citations.


class Claim(BaseModel):
    text: str
    evidence_ids: list[str]
    # "unverified": the model said supported, but none of its citations survived validation.
    status: Literal["supported", "hypothesis", "unverified"]


class Answer(BaseModel):
    summary: str
    claims: list[Claim]
    limitations: list[str]
    suggested_next_steps: list[str]


class EvidenceOut(BaseModel):
    id: str
    chunk_id: uuid.UUID
    path: str
    language: str | None
    start_line: int
    end_line: int
    symbol_name: str | None
    retrieved_by: list[str]
    score: float
    text: str


class SearchResponse(BaseModel):
    snapshot_id: uuid.UUID
    commit_sha: str
    items: list[EvidenceOut]


class InvestigationRequest(BaseModel):
    question: str = Field(min_length=3, max_length=2000)
    snapshot_id: uuid.UUID | None = None


class InvestigationOut(BaseModel):
    id: uuid.UUID
    repository_id: uuid.UUID
    snapshot_id: uuid.UUID
    commit_sha: str
    question: str
    status: str
    answer: Answer | None
    evidence: list[EvidenceOut]
    validation: dict[str, Any]
    error_code: str | None
    error_message: str | None
    metadata: dict[str, Any]
    created_at: datetime
    finished_at: datetime | None


class InvestigationSummary(ORMModel):
    id: uuid.UUID
    snapshot_id: uuid.UUID
    question: str
    status: str
    created_at: datetime


class InvestigationList(BaseModel):
    items: list[InvestigationSummary]
    next_cursor: str | None
