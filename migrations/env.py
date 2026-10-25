import asyncio
import os

from alembic import context
from sqlalchemy.engine import Connection

from deadline_assistant.config import DatabaseSettings
from deadline_assistant.db.base import Base
from deadline_assistant.db.models import User  # noqa: F401
from deadline_assistant.db.session import Database

os.umask(0o077)


def configure(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=Base.metadata,
        compare_type=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def online() -> None:
    database = Database(DatabaseSettings())
    try:
        async with database.engine.connect() as connection:
            await connection.run_sync(configure)
            # PRAGMA в SQLite может открыть внешнюю транзакцию до Alembic.
            await connection.commit()
    finally:
        await database.close()


if context.is_offline_mode():
    context.configure(
        url=DatabaseSettings().database_url.get_secret_value(),
        target_metadata=Base.metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )
    with context.begin_transaction():
        context.run_migrations()
else:
    asyncio.run(online())
