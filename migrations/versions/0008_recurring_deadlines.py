"""User-defined recurring deadlines and their ordinary event occurrences."""

import sqlalchemy as sa
from alembic import op

revision = "h108_recurring"
down_revision = "g107_initial_choice"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "recurring_deadlines",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("interval_days", sa.Integer(), nullable=False),
        sa.Column("first_due_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(100), nullable=False),
        sa.Column("has_time", sa.Boolean(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False, server_default="1"),
        sa.CheckConstraint("interval_days BETWEEN 1 AND 14", name="recurring_interval_range"),
    )
    op.create_index("ix_recurring_deadlines_user_id", "recurring_deadlines", ["user_id"])
    if op.get_bind().dialect.name == "sqlite":
        # A nullable inline reference avoids rebuilding events and cascading deletion
        # of their existing sources/corrections on installations with foreign_keys=ON.
        op.execute(
            "ALTER TABLE events ADD COLUMN recurring_deadline_id CHAR(32) "
            "REFERENCES recurring_deadlines(id)"
        )
    else:
        op.add_column(
            "events",
            sa.Column(
                "recurring_deadline_id",
                sa.Uuid(),
                sa.ForeignKey("recurring_deadlines.id"),
                nullable=True,
            ),
        )
    op.create_index("ix_events_recurring_deadline_id", "events", ["recurring_deadline_id"])
    op.create_index(
        "uq_events_recurring_due", "events", ["recurring_deadline_id", "due_at"], unique=True
    )


def downgrade() -> None:
    op.drop_index("uq_events_recurring_due", table_name="events")
    op.drop_index("ix_events_recurring_deadline_id", table_name="events")
    op.drop_column("events", "recurring_deadline_id")
    op.drop_table("recurring_deadlines")
