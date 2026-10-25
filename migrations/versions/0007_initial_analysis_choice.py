"""Mandatory onboarding and a durable one-time history choice."""

import sqlalchemy as sa
from alembic import op

revision = "g107_initial_choice"
down_revision = "f106_study_group"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("initial_analysis_choice", sa.Boolean(), nullable=True))
    op.add_column(
        "users", sa.Column("initial_analysis_decided_at", sa.DateTime(timezone=True), nullable=True)
    )
    op.add_column(
        "tracked_chats", sa.Column("history_after", sa.DateTime(timezone=True), nullable=True)
    )
    # Existing runs already used the opportunity, including failed/interrupted attempts.
    op.execute(
        sa.text(
            "UPDATE users SET initial_analysis_choice = TRUE, initial_analysis_decided_at = "
            "(SELECT MIN(started_at) FROM analysis_runs WHERE analysis_runs.user_id = users.id) "
            "WHERE EXISTS (SELECT 1 FROM analysis_runs WHERE analysis_runs.user_id = users.id)"
        )
    )


def downgrade() -> None:
    op.drop_column("tracked_chats", "history_after")
    op.drop_column("users", "initial_analysis_decided_at")
    op.drop_column("users", "initial_analysis_choice")
