"""graph edges

Revision ID: 0004
Revises: 0003
Create Date: 2026-10-19 18:14:34.856371

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# revision identifiers, used by Alembic.
revision: str = "0004"
down_revision: str | Sequence[str] | None = "0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "graph_edges",
        sa.Column("snapshot_id", sa.Uuid(), nullable=False),
        sa.Column("relation", sa.String(length=20), nullable=False),
        sa.Column("source_file_id", sa.Uuid(), nullable=False),
        sa.Column("source_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("target_file_id", sa.Uuid(), nullable=False),
        sa.Column("target_symbol_id", sa.Uuid(), nullable=True),
        sa.Column("line", sa.Integer(), nullable=False),
        sa.Column("resolution", sa.String(length=10), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("provenance", sa.String(length=80), nullable=False),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["repository_snapshots.id"],
            name=op.f("fk_graph_edges_snapshot_id_repository_snapshots"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_file_id"],
            ["files.id"],
            name=op.f("fk_graph_edges_source_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["source_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_graph_edges_source_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_file_id"],
            ["files.id"],
            name=op.f("fk_graph_edges_target_file_id_files"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["target_symbol_id"],
            ["symbols.id"],
            name=op.f("fk_graph_edges_target_symbol_id_symbols"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_graph_edges")),
    )
    op.create_index(
        "ix_graph_edges_snapshot_source_file",
        "graph_edges",
        ["snapshot_id", "source_file_id"],
        unique=False,
    )
    op.create_index(
        "ix_graph_edges_snapshot_source_symbol",
        "graph_edges",
        ["snapshot_id", "source_symbol_id"],
        unique=False,
    )
    op.create_index(
        "ix_graph_edges_snapshot_target_file",
        "graph_edges",
        ["snapshot_id", "target_file_id"],
        unique=False,
    )
    op.create_index(
        "ix_graph_edges_snapshot_target_symbol",
        "graph_edges",
        ["snapshot_id", "target_symbol_id"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_graph_edges_snapshot_target_symbol", table_name="graph_edges")
    op.drop_index("ix_graph_edges_snapshot_target_file", table_name="graph_edges")
    op.drop_index("ix_graph_edges_snapshot_source_symbol", table_name="graph_edges")
    op.drop_index("ix_graph_edges_snapshot_source_file", table_name="graph_edges")
    op.drop_table("graph_edges")
