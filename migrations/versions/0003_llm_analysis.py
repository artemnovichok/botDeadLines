"""Структурированный анализ, версии событий, периоды досдачи и доступность источников."""

import sqlalchemy as sa
from alembic import op

revision = "c932_llm_analysis"
down_revision = "b821_connection"
branch_labels = None
depends_on = None


def columns() -> dict[str, list[sa.Column]]:
    return {
        "events": [
            sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("end_at", sa.DateTime(timezone=True)),
            sa.Column("schedule_kind", sa.String(30), nullable=False, server_default="deadline"),
            sa.Column(
                "needs_clarification", sa.Boolean(), nullable=False, server_default=sa.false()
            ),
            sa.Column("submission_conditions", sa.Text(), nullable=False, server_default=""),
            sa.Column("penalties", sa.Text(), nullable=False, server_default=""),
            sa.Column("resubmission_start", sa.DateTime(timezone=True)),
            sa.Column("resubmission_end", sa.DateTime(timezone=True)),
            sa.Column(
                "resubmission_precision", sa.String(30), nullable=False, server_default="day"
            ),
            sa.Column("dedup_key", sa.String(64)),
        ],
        "source_messages": [
            sa.Column("chat_title", sa.String(300), nullable=False, server_default=""),
            sa.Column("reply_to_message_id", sa.BigInteger()),
            sa.Column("available", sa.Boolean(), nullable=False, server_default=sa.true()),
            sa.Column("checked_at", sa.DateTime(timezone=True)),
        ],
        "user_corrections": [
            sa.Column("telegram_chat_id", sa.BigInteger()),
            sa.Column("telegram_message_id", sa.BigInteger()),
        ],
        "conflicts": [
            sa.Column("event_version", sa.Integer(), nullable=False, server_default="1"),
            sa.Column("reason", sa.Text(), nullable=False, server_default=""),
        ],
        "conflict_options": [
            sa.Column("correction_ids", sa.JSON(), nullable=False, server_default="[]"),
            sa.Column("label", sa.String(300), nullable=False, server_default=""),
        ],
    }


def upgrade() -> None:
    for table, additions in columns().items():
        for column in additions:
            op.add_column(table, column)
    op.create_index("ix_events_dedup_key", "events", ["dedup_key"])
    # Legacy options were prepared without a version; they must be rechecked before selection.
    op.execute(
        sa.text("UPDATE conflicts SET event_version = 0 WHERE status IN ('open', 'unconfirmed')")
    )


def downgrade() -> None:
    op.drop_index("ix_events_dedup_key", table_name="events")
    for table, additions in reversed(list(columns().items())):
        for column in reversed(additions):
            op.drop_column(table, column.name)
