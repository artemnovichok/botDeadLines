import asyncio
import os
import sys
from pathlib import Path

from sqlalchemy import inspect
from sqlalchemy.ext.asyncio import create_async_engine


async def alembic(database_url: str, *arguments: str) -> str:
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "alembic",
        *arguments,
        env={**os.environ, "DATABASE_URL": database_url},
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0, output.decode()
    return output.decode()


async def test_migrations_upgrade_check_downgrade(tmp_path: Path) -> None:
    url = f"sqlite+aiosqlite:///{tmp_path}/migrations.db"
    await alembic(url, "upgrade", "head")
    await alembic(url, "check")
    engine = create_async_engine(url)
    async with engine.connect() as connection:
        tables = await connection.run_sync(lambda sync: inspect(sync).get_table_names())
    assert len(tables) == 21  # 20 прикладных таблиц + alembic_version
    assert "recurring_deadlines" in tables
    await engine.dispose()
    await alembic(url, "downgrade", "base")
    await alembic(url, "upgrade", "head")


async def test_postgresql_migration_sql_compiles() -> None:
    sql = await alembic("postgresql+asyncpg://test:test@localhost/test", "upgrade", "head", "--sql")
    assert "CREATE TABLE users" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "FOREIGN KEY(event_id, user_id)" in sql
    assert "CREATE TABLE recurring_deadlines" in sql
    assert "recurring_deadline_id" in sql


async def test_second_stage_upgrade_preserves_existing_deadlines_and_session(tmp_path: Path):
    from pydantic import SecretStr

    from deadline_assistant.config import Settings
    from deadline_assistant.db.session import Database
    from deadline_assistant.repositories.tenant import TenantRepository

    url = f"sqlite+aiosqlite:///{tmp_path}/existing.db"
    await alembic(url, "upgrade", "a7fda3725dfd")
    settings = Settings(
        _env_file=None,
        **{
            **Settings.model_construct().model_dump(),
            "database_url": SecretStr(url),
        },
    )
    from uuid import uuid4

    from sqlalchemy import text

    user_id, event_id, session_id, conflict_id = uuid4(), uuid4(), uuid4(), uuid4()
    new_user_id = uuid4()
    engine = create_async_engine(url)
    async with engine.begin() as connection:
        await connection.execute(
            text(
                "INSERT INTO users (id, telegram_user_id, timezone, preferred_digest_time, "
                "enabled, created_at) VALUES (:id, 101, 'Europe/Moscow', '08:00:00', "
                "1, '2026-10-20 00:00:00')"
            ),
            {"id": user_id.hex},
        )
        await connection.execute(
            text(
                "INSERT INTO users (id, telegram_user_id, timezone, preferred_digest_time, "
                "enabled, created_at) VALUES (:id, 202, 'Europe/Moscow', '08:00:00', "
                "1, '2026-10-20 00:00:00')"
            ),
            {"id": new_user_id.hex},
        )
        await connection.execute(
            text(
                "INSERT INTO analysis_runs (id, user_id, started_at, status, model, token_usage) "
                "VALUES (:id, :user, '2026-10-20 00:00:00', 'failed', 'mock', '{}')"
            ),
            {"id": uuid4().hex, "user": user_id.hex},
        )
        await connection.execute(
            text(
                "INSERT INTO events (id, user_id, title, description, event_type, timezone, "
                "date_precision, status, is_optional, confidence, created_at, updated_at) "
                "VALUES (:id, :user, 'Сохранён до обновления', '', 'deadline', 'Europe/Moscow', "
                "'unknown', 'active', 0, 0, '2026-10-20 00:00:00', '2026-10-20 00:00:00')"
            ),
            {"id": event_id.hex, "user": user_id.hex},
        )
        await connection.execute(
            text(
                "INSERT INTO telegram_sessions (id, user_id, encrypted_session_data, status, "
                "connected_at) VALUES (:id, :user, 'existing-encrypted-session', 'connected', "
                "'2026-10-20 00:00:00')"
            ),
            {"id": session_id.hex, "user": user_id.hex},
        )
        await connection.execute(
            text(
                "INSERT INTO conflicts (id, user_id, event_id, conflict_type, status, created_at) "
                "VALUES (:id, :user, :event, 'event_change', 'open', '2026-10-20 00:00:00')"
            ),
            {"id": conflict_id.hex, "user": user_id.hex, "event": event_id.hex},
        )
    await engine.dispose()
    database = Database(settings)
    try:
        await alembic(url, "upgrade", "head")
        await alembic(url, "check")
        async with database.transaction() as session:
            repo = TenantRepository(session, user_id)
            from deadline_assistant.repositories.users import UserRepository

            user = await UserRepository(session).require(user_id)
            assert user.study_group is None and user.study_group_prompted_at is None
            assert user.initial_analysis_choice is True
            assert user.initial_analysis_decided_at is not None
            assert (
                await UserRepository(session).require(new_user_id)
            ).initial_analysis_choice is None
            await UserRepository(session).set_study_group(user_id, "БПИ267")
            event = await repo.event(event_id)
            assert event.title == "Сохранён до обновления"
            assert event.recurring_deadline_id is None
            assert (await repo.conflict(conflict_id)).event_version == 0
            assert (
                await repo.telegram_session()
            ).encrypted_session_data == "existing-encrypted-session"
    finally:
        await database.close()
