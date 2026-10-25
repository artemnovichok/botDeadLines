"""Persistent study group and one-time onboarding prompt."""

import sqlalchemy as sa
from alembic import op

revision = "f106_study_group"
down_revision = "e105_operations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("users", sa.Column("study_group", sa.String(32), nullable=True))
    op.add_column(
        "users", sa.Column("study_group_prompted_at", sa.DateTime(timezone=True), nullable=True)
    )


def downgrade() -> None:
    op.drop_column("users", "study_group_prompted_at")
    op.drop_column("users", "study_group")
