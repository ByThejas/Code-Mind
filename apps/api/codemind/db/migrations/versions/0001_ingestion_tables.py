"""ingestion tables

Revision ID: 0001
Revises:
Create Date: 2026-10-16 10:04:55.687770

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.create_table(
        "workspaces",
        sa.Column("name", sa.String(length=200), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_workspaces")),
    )
    op.create_table(
        "repositories",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("provider", sa.String(length=20), nullable=False),
        sa.Column("owner", sa.String(length=100), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("default_branch", sa.String(length=255), nullable=False),
        sa.Column("visibility", sa.String(length=20), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_repositories_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repositories")),
    )
    op.create_index(
        op.f("ix_repositories_workspace_id"), "repositories", ["workspace_id"], unique=False
    )
    op.create_index(
        "uq_repositories_workspace_provider_owner_name",
        "repositories",
        [
            "workspace_id",
            "provider",
            sa.literal_column("lower(owner)"),
            sa.literal_column("lower(name)"),
        ],
        unique=True,
    )
    op.create_table(
        "repository_snapshots",
        sa.Column("repository_id", sa.Uuid(), nullable=False),
        sa.Column("commit_sha", sa.String(length=40), nullable=False),
        sa.Column("requested_ref", sa.String(length=255), nullable=True),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("warnings", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("indexed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.CheckConstraint(
            "commit_sha ~ '^[0-9a-f]{40}$'", name=op.f("ck_repository_snapshots_commit_sha_format")
        ),
        sa.ForeignKeyConstraint(
            ["repository_id"],
            ["repositories.id"],
            name=op.f("fk_repository_snapshots_repository_id_repositories"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_repository_snapshots")),
        sa.UniqueConstraint(
            "repository_id",
            "commit_sha",
            name=op.f("uq_repository_snapshots_repository_id_commit_sha"),
        ),
    )
    op.create_table(
        "files",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("path", sa.Text(), nullable=False),
        sa.Column("language", sa.String(length=40), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("line_count", sa.Integer(), nullable=False),
        sa.Column("excluded_reason", sa.String(length=40), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_files_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_files")),
        sa.UniqueConstraint("snapshot_id", "path", name=op.f("uq_files_snapshot_id_path")),
    )
    op.create_table(
        "jobs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=True),
        sa.Column("type", sa.String(length=40), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("stage", sa.String(length=20), nullable=True),
        sa.Column("progress", sa.Integer(), nullable=False),
        sa.Column("attempt", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_jobs_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_jobs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_jobs")),
    )
    op.create_index(op.f("ix_jobs_workspace_id"), "jobs", ["workspace_id"], unique=False)
    op.create_index(
        "uq_jobs_active_snapshot",
        "jobs",
        ["snapshot_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.create_table(
        "chunks",
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("ordinal", sa.Integer(), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("symbol_name", sa.Text(), nullable=True),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("token_estimate", sa.Integer(), nullable=False),
        sa.Column(
            "search_tsv",
            postgresql.TSVECTOR(),
            sa.Computed("to_tsvector('simple', text)", persisted=True),
            nullable=False,
        ),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name=op.f("fk_chunks_file_id_files"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_chunks_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_chunks")),
        sa.UniqueConstraint("file_id", "ordinal", name=op.f("uq_chunks_file_id_ordinal")),
    )
    op.create_index(
        "ix_chunks_search_tsv", "chunks", ["search_tsv"], unique=False, postgresql_using="gin"
    )
    op.create_index(op.f("ix_chunks_snapshot_id"), "chunks", ["snapshot_id"], unique=False)
    op.create_table(
        "file_contents",
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"],
            ["files.id"],
            name=op.f("fk_file_contents_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("file_id", name=op.f("pk_file_contents")),
    )
    op.create_table(
        "symbols",
        sa.Column("file_id", sa.Uuid(), nullable=False),
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("parent_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("name", sa.Text(), nullable=False),
        sa.Column("qualified_name", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("start_line", sa.Integer(), nullable=False),
        sa.Column("end_line", sa.Integer(), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["file_id"], ["files.id"], name=op.f("fk_symbols_file_id_files"), ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["parent_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_symbols_parent_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_symbols_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_symbols")),
    )
    op.create_index("ix_symbols_snapshot_id_name", "symbols", ["snapshot_id", "name"], unique=False)
    op.create_table(
        "embeddings",
        sa.Column("chunk_id", sa.Uuid(), nullable=False),
        sa.Column("model_name", sa.String(length=200), nullable=False),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("embedding", Vector(1024), nullable=False),  # must match EMBEDDING_DIMENSIONS
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["chunk_id"],
            ["chunks.id"],
            name=op.f("fk_embeddings_chunk_id_chunks"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("chunk_id", name=op.f("pk_embeddings")),
    )
    op.create_index(
        "ix_embeddings_embedding_hnsw",
        "embeddings",
        ["embedding"],
        unique=False,
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    # Single workspace until authentication exists (see codemind.db.models.DEFAULT_WORKSPACE_ID).
    op.execute(
        "INSERT INTO workspaces (id, name, created_at) "
        "VALUES ('00000000-0000-0000-0000-000000000001', 'Default', now())"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_embeddings_embedding_hnsw",
        table_name="embeddings",
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )
    op.drop_table("embeddings")
    op.drop_index("ix_symbols_snapshot_id_name", table_name="symbols")
    op.drop_table("symbols")
    op.drop_table("file_contents")
    op.drop_index(op.f("ix_chunks_snapshot_id"), table_name="chunks")
    op.drop_index("ix_chunks_search_tsv", table_name="chunks", postgresql_using="gin")
    op.drop_table("chunks")
    op.drop_index(
        "uq_jobs_active_snapshot",
        table_name="jobs",
        postgresql_where=sa.text("status IN ('queued', 'running')"),
    )
    op.drop_index(op.f("ix_jobs_workspace_id"), table_name="jobs")
    op.drop_table("jobs")
    op.drop_table("files")
    op.drop_table("repository_snapshots")
    op.drop_index("uq_repositories_workspace_provider_owner_name", table_name="repositories")
    op.drop_index(op.f("ix_repositories_workspace_id"), table_name="repositories")
    op.drop_table("repositories")
    op.drop_table("workspaces")
