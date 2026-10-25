"""Run against a disposable PostgreSQL server; each test creates and drops its own database."""

import asyncio
import os
from uuid import uuid4

import asyncpg
import pytest
from pydantic import SecretStr
from sqlalchemy import select
from sqlalchemy.engine import make_url
from test_migrations import alembic
from test_operations import pipeline, prepare

from deadline_assistant.config import Settings
from deadline_assistant.db.models import AnalysisRun
from deadline_assistant.db.schema import check_schema
from deadline_assistant.db.session import Database
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.coordination import JobBusy
from deadline_assistant.services.cycle import CycleService
from deadline_assistant.services.requests import throttle

pytestmark = [
    pytest.mark.postgres,
    pytest.mark.skipif(
        not os.getenv("POSTGRES_TEST_URL"), reason="Нужен изолированный POSTGRES_TEST_URL"
    ),
]


@pytest.fixture
async def pg_database(configured):
    url = make_url(os.environ["POSTGRES_TEST_URL"])
    admin = await asyncpg.connect(
        url.set(drivername="postgresql").render_as_string(hide_password=False)
    )
    name = "deadline_test_" + uuid4().hex
    await admin.execute(f'CREATE DATABASE "{name}"')  # Generated identifier, never user input.
    test_url = url.set(database=name).render_as_string(hide_password=False)
    settings = Settings(
        _env_file=None, **{**configured.model_dump(), "database_url": SecretStr(test_url)}
    )
    database = Database(settings)
    try:
        await alembic(test_url, "upgrade", "head")
        await alembic(test_url, "check")
        async with database.transaction() as session:
            user = await UserRepository(session).get_or_create(101, settings)
        yield database, settings, user
    finally:
        await database.close()
        await admin.execute(f'DROP DATABASE "{name}" WITH (FORCE)')
        await admin.close()


async def test_postgres_migrations_cycle_and_readonly_preview(pg_database):
    database, settings, user = pg_database
    await prepare(database, user)
    sync, analyzer = pipeline(database, settings)
    readonly = Database(settings, readonly=True)
    try:
        await check_schema(readonly)
        result = await CycleService(readonly, settings, sync, None).preview(user)
        assert result.events == 1 and result.proposals
    finally:
        await readonly.close()
    assert await sync.sync_user(user.id) == 1
    assert await sync.sync_user(user.id) == 0
    async with database.transaction() as session:
        runs = list(await session.scalars(select(AnalysisRun)))
        assert all(run.status == "succeeded" for run in runs)
    assert analyzer.analyze.await_count == 2  # Preview and one committed analysis.
    await alembic(settings.database_url.get_secret_value(), "downgrade", "base")
    await alembic(settings.database_url.get_secret_value(), "upgrade", "head")
    await alembic(settings.database_url.get_secret_value(), "check")


async def test_postgres_shared_lock_and_atomic_throttle(pg_database):
    database, settings, user = pg_database
    other = Database(settings)
    try:
        async with database.lock("shared-lock"):
            with pytest.raises(JobBusy):
                async with other.lock("shared-lock", wait=False):
                    pytest.fail("The advisory lock must exclude other connections")
            async with database.lock("shared-lock"):  # Same task is reentrant.
                pass
        async with other.lock("shared-lock", wait=False):
            pass
        started = asyncio.get_running_loop().time()
        await asyncio.gather(*(throttle(db, "shared-api", 0.02) for db in [database, other] * 3))
        assert asyncio.get_running_loop().time() - started >= 0.09
    finally:
        await other.close()


async def test_postgres_parallel_cycle_no_duplicates(pg_database):
    database, settings, user = pg_database
    await prepare(database, user)
    sync, analyzer = pipeline(database, settings)
    other = Database(settings)
    from deadline_assistant.services.sync import SyncService

    try:
        second = SyncService(other, sync.reader, analyzer, settings)
        assert sorted(await asyncio.gather(sync.sync_user(user.id), second.sync_user(user.id))) == [
            0,
            1,
        ]
        analyzer.analyze.assert_awaited_once()
    finally:
        await other.close()
