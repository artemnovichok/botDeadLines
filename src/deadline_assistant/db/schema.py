from sqlalchemy import text

from deadline_assistant.db.session import Database

SCHEMA_REVISION = "h108_recurring"


async def check_schema(database: Database) -> None:
    async with database.engine.connect() as connection:
        revisions: set[str] = set(
            (await connection.execute(text("SELECT version_num FROM alembic_version"))).scalars()
        )
        if revisions != {SCHEMA_REVISION}:
            raise ValueError("Схема БД устарела. Выполните alembic upgrade head")
