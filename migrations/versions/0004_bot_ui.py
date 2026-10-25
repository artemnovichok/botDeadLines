"""Versioned lists, correction undo, completion protection and resumable delivery."""

import sqlalchemy as sa
from alembic import op

revision = "d104_bot_ui"
down_revision = "c932_llm_analysis"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("bot_blocked_at", sa.DateTime(timezone=True)))
    op.add_column(
        "events",
        sa.Column("completed_by_user", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.execute(sa.text("UPDATE events SET completed_by_user = true WHERE status = 'completed'"))
    for name in ("previous_data", "applied_data"):
        op.add_column("user_corrections", sa.Column(name, sa.JSON()))
    op.add_column("scheduled_deliveries", sa.Column("payload", sa.JSON()))
    op.add_column(
        "scheduled_deliveries",
        sa.Column("next_part", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_table(
        "event_lists",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("entries", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_event_lists_user_id", "event_lists", ["user_id"])
    op.create_index("ix_event_lists_expires_at", "event_lists", ["expires_at"])
    op.create_table(
        "callback_receipts",
        sa.Column("key", sa.String(64), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_callback_receipts_user_id", "callback_receipts", ["user_id"])
    op.create_index("ix_callback_receipts_created_at", "callback_receipts", ["created_at"])
    op.create_table(
        "event_tombstones",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("dedup_key", sa.String(64), nullable=False),
        sa.UniqueConstraint("user_id", "dedup_key"),
    )
    op.create_index("ix_event_tombstones_user_id", "event_tombstones", ["user_id"])


def downgrade() -> None:
    for table in ("event_tombstones", "callback_receipts", "event_lists"):
        op.drop_table(table)
    op.drop_column("scheduled_deliveries", "next_part")
    op.drop_column("scheduled_deliveries", "payload")
    op.drop_column("user_corrections", "applied_data")
    op.drop_column("user_corrections", "previous_data")
    op.drop_column("events", "completed_by_user")
    op.drop_column("users", "bot_blocked_at")
