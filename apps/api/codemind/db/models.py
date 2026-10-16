"""Ingestion tables (blueprint §8). Everything under a repository cascades on delete."""

import uuid
from datetime import datetime
from enum import StrEnum
from typing import Any

from pgvector.sqlalchemy import Vector
from sqlalchemy import (
    Boolean,
    CheckConstraint,
    Computed,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, TSVECTOR
from sqlalchemy.orm import Mapped, mapped_column, relationship

from codemind.core.config import settings
from codemind.db.base import Base, UUIDPrimaryKey, utcnow

DEFAULT_WORKSPACE_ID = uuid.UUID("00000000-0000-0000-0000-000000000001")


class SnapshotStatus(StrEnum):
    QUEUED = "queued"
    FETCHING = "fetching"
    INDEXING = "indexing"
    READY = "ready"
    PARTIAL = "partial"
    FAILED = "failed"
    CANCELLED = "cancelled"


SERVABLE_SNAPSHOT_STATUSES = (SnapshotStatus.READY, SnapshotStatus.PARTIAL)


class JobStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


ACTIVE_JOB_STATUSES = (JobStatus.QUEUED, JobStatus.RUNNING)


class Workspace(UUIDPrimaryKey, Base):
    __tablename__ = "workspaces"

    name: Mapped[str] = mapped_column(String(200))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Repository(UUIDPrimaryKey, Base):
    __tablename__ = "repositories"
    __table_args__ = (
        Index(
            "uq_repositories_workspace_provider_owner_name",
            "workspace_id",
            "provider",
            func.lower(text("owner")),
            func.lower(text("name")),
            unique=True,
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    provider: Mapped[str] = mapped_column(String(20), default="github")
    owner: Mapped[str] = mapped_column(String(100))
    name: Mapped[str] = mapped_column(String(100))
    default_branch: Mapped[str] = mapped_column(String(255))
    visibility: Mapped[str] = mapped_column(String(20), default="public")
    created_at: Mapped[datetime] = mapped_column(default=utcnow)

    snapshots: Mapped[list["RepositorySnapshot"]] = relationship(
        back_populates="repository", cascade="all, delete-orphan", passive_deletes=True
    )


class RepositorySnapshot(UUIDPrimaryKey, Base):
    __tablename__ = "repository_snapshots"
    __table_args__ = (
        UniqueConstraint("repository_id", "commit_sha"),
        CheckConstraint("commit_sha ~ '^[0-9a-f]{40}$'", name="commit_sha_format"),
    )

    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    commit_sha: Mapped[str] = mapped_column(String(40))
    requested_ref: Mapped[str | None] = mapped_column(String(255))
    status: Mapped[str] = mapped_column(String(20), default=SnapshotStatus.QUEUED)
    stats: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    warnings: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    indexed_at: Mapped[datetime | None]

    repository: Mapped[Repository] = relationship(back_populates="snapshots")


class File(UUIDPrimaryKey, Base):
    __tablename__ = "files"
    __table_args__ = (UniqueConstraint("snapshot_id", "path"),)

    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE")
    )
    path: Mapped[str] = mapped_column(Text)
    language: Mapped[str | None] = mapped_column(String(40))
    size_bytes: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str | None] = mapped_column(String(64))
    line_count: Mapped[int] = mapped_column(Integer, default=0)
    excluded_reason: Mapped[str | None] = mapped_column(String(40))


class FileContent(Base):
    __tablename__ = "file_contents"

    file_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("files.id", ondelete="CASCADE"), primary_key=True
    )
    text: Mapped[str] = mapped_column(Text)


class Chunk(UUIDPrimaryKey, Base):
    __tablename__ = "chunks"
    __table_args__ = (
        UniqueConstraint("file_id", "ordinal"),
        Index("ix_chunks_search_tsv", "search_tsv", postgresql_using="gin"),
    )

    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE"), index=True
    )
    ordinal: Mapped[int] = mapped_column(Integer)
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)
    kind: Mapped[str] = mapped_column(String(20))
    symbol_name: Mapped[str | None] = mapped_column(Text)
    text: Mapped[str] = mapped_column(Text)
    token_estimate: Mapped[int] = mapped_column(Integer)
    search_tsv: Mapped[str] = mapped_column(
        TSVECTOR, Computed("to_tsvector('simple', text)", persisted=True)
    )


class Symbol(UUIDPrimaryKey, Base):
    __tablename__ = "symbols"
    __table_args__ = (Index("ix_symbols_snapshot_id_name", "snapshot_id", "name"),)

    file_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("files.id", ondelete="CASCADE"))
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE")
    )
    parent_symbol_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("symbols.id", ondelete="CASCADE")
    )
    name: Mapped[str] = mapped_column(Text)
    qualified_name: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(String(20))
    start_line: Mapped[int] = mapped_column(Integer)
    end_line: Mapped[int] = mapped_column(Integer)


class Embedding(Base):
    """Written by the index job's embed stage. The dimension is fixed by EMBEDDING_DIMENSIONS."""

    __tablename__ = "embeddings"
    __table_args__ = (
        Index(
            "ix_embeddings_embedding_hnsw",
            "embedding",
            postgresql_using="hnsw",
            postgresql_ops={"embedding": "vector_cosine_ops"},
        ),
    )

    chunk_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True
    )
    model_name: Mapped[str] = mapped_column(String(200))
    dimensions: Mapped[int] = mapped_column(Integer)
    embedding: Mapped[list[float]] = mapped_column(Vector(settings.embedding_dimensions))
    created_at: Mapped[datetime] = mapped_column(default=utcnow)


class Job(UUIDPrimaryKey, Base):
    __tablename__ = "jobs"
    __table_args__ = (
        # At most one queued or running job per snapshot: concurrent imports share it.
        Index(
            "uq_jobs_active_snapshot",
            "snapshot_id",
            unique=True,
            postgresql_where=text("status IN ('queued', 'running')"),
        ),
    )

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    snapshot_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE")
    )
    type: Mapped[str] = mapped_column(String(40), default="index_snapshot")
    status: Mapped[str] = mapped_column(String(20), default=JobStatus.QUEUED)
    stage: Mapped[str | None] = mapped_column(String(20))
    progress: Mapped[int] = mapped_column(Integer, default=0)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=4)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    started_at: Mapped[datetime | None]
    finished_at: Mapped[datetime | None]
    # Written by the worker while it runs; a stale value means the worker died (indexing/recovery.py).
    heartbeat_at: Mapped[datetime | None]


class InvestigationStatus(StrEnum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"


class Investigation(UUIDPrimaryKey, Base):
    """One question asked of a snapshot, with its validated answer, evidence and audit metadata."""

    __tablename__ = "investigations"
    __table_args__ = (Index("ix_investigations_repository_created", "repository_id", "created_at"),)

    workspace_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("workspaces.id", ondelete="CASCADE"), index=True
    )
    repository_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repositories.id", ondelete="CASCADE")
    )
    snapshot_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("repository_snapshots.id", ondelete="CASCADE")
    )
    question: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(20))
    answer: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    evidence: Mapped[list[dict[str, Any]]] = mapped_column(JSONB, default=list)
    validation: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    error_code: Mapped[str | None] = mapped_column(String(60))
    error_message: Mapped[str | None] = mapped_column(Text)
    # Prompt version, models, retrieval settings, candidate counts, tokens, duration (FR-11).
    metadata_: Mapped[dict[str, Any]] = mapped_column("metadata", JSONB, default=dict)
    created_at: Mapped[datetime] = mapped_column(default=utcnow)
    finished_at: Mapped[datetime | None]
