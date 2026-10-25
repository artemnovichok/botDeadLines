"""Подтверждение веб-входа и журнал фонового анализа."""

import sqlalchemy as sa
from alembic import op

revision = "b821_connection"
down_revision = "a7fda3725dfd"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("tracked_chats", sa.Column("initial_upper_message_id", sa.BigInteger()))
    op.add_column("auth_links", sa.Column("approved_at", sa.DateTime(timezone=True)))
    op.add_column(
        "auth_links", sa.Column("attempt_count", sa.Integer(), nullable=False, server_default="0")
    )
    op.add_column(
        "analysis_runs", sa.Column("kind", sa.String(30), nullable=False, server_default="sync")
    )
    for name in ("chat_count", "message_count", "event_count"):
        op.add_column(
            "analysis_runs", sa.Column(name, sa.Integer(), nullable=False, server_default="0")
        )
    # Незавершённые ссылки предыдущей версии не обходят подтверждение в боте.
    op.execute(sa.text("DELETE FROM auth_links"))


def downgrade() -> None:
    op.drop_column("tracked_chats", "initial_upper_message_id")
    for name in ("event_count", "message_count", "chat_count", "kind"):
        op.drop_column("analysis_runs", name)
    op.drop_column("auth_links", "attempt_count")
    op.drop_column("auth_links", "approved_at")
