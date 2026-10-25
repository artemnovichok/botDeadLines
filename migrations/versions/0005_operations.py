"""Durable analysis batches, shared request budgets and cycle metrics."""

import sqlalchemy as sa
from alembic import op

revision = "e105_operations"
down_revision = "d104_bot_ui"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for name in ("conflict_count", "error_count", "new_message_count"):
        op.add_column(
            "analysis_runs", sa.Column(name, sa.Integer(), nullable=False, server_default="0")
        )
    op.add_column(
        "analysis_runs",
        sa.Column(
            "duration_seconds",
            sa.Float(),
            nullable=False,
            server_default="0",
        ),
    )
    op.create_table(
        "analysis_batches",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("fingerprint", sa.String(64), nullable=False),
        sa.Column("output", sa.JSON(), nullable=False),
        sa.Column("token_usage", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("user_id", "fingerprint"),
    )
    op.create_index("ix_analysis_batches_user_id", "analysis_batches", ["user_id"])
    op.create_index("ix_analysis_batches_created_at", "analysis_batches", ["created_at"])
    op.create_table(
        "request_budgets",
        sa.Column("key", sa.String(100), primary_key=True),
        sa.Column("next_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade() -> None:
    op.drop_table("request_budgets")
    op.drop_table("analysis_batches")
    for name in ("duration_seconds", "new_message_count", "error_count", "conflict_count"):
        op.drop_column("analysis_runs", name)
