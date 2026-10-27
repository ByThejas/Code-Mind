"""test runs

Revision ID: 0006
Revises: 0005
Create Date: 2026-10-26 17:00:07.986242

"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# revision identifiers, used by Alembic.
revision: str = "0006"
down_revision: str | Sequence[str] | None = "0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "test_runs",
        sa.Column("workspace_id", sa.Uuid(), nullable=False),
        sa.Column("patch_id", sa.Uuid(), nullable=False),
        sa.Column("profile", sa.String(length=40), nullable=False),
        sa.Column("diff_sha256", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("image", sa.String(length=300), nullable=False),
        sa.Column("command", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("limits", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("exit_code", sa.Integer(), nullable=True),
        sa.Column("duration_ms", sa.Integer(), nullable=True),
        sa.Column("output", sa.Text(), nullable=True),
        sa.Column("output_truncated", sa.Boolean(), nullable=False),
        sa.Column("workspace_stats", postgresql.JSONB(astext_type=sa.Text()), nullable=False),
        sa.Column("error_code", sa.String(length=60), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.ForeignKeyConstraint(
            ["patch_id"],
            ["patches.id"],
            name=op.f("fk_test_runs_patch_id_patches"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["workspace_id"],
            ["workspaces.id"],
            name=op.f("fk_test_runs_workspace_id_workspaces"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_test_runs")),
    )
    op.create_index(
        "ix_test_runs_patch_created", "test_runs", ["patch_id", "created_at"], unique=False
    )
    op.create_index(op.f("ix_test_runs_workspace_id"), "test_runs", ["workspace_id"], unique=False)


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(op.f("ix_test_runs_workspace_id"), table_name="test_runs")
    op.drop_index("ix_test_runs_patch_created", table_name="test_runs")
    op.drop_table("test_runs")
