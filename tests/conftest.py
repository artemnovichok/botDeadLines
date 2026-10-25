from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from cryptography.fernet import Fernet
from pydantic import SecretStr
from sqlalchemy import text

from deadline_assistant.config import Settings
from deadline_assistant.db.base import Base
from deadline_assistant.db.models import User
from deadline_assistant.db.schema import SCHEMA_REVISION
from deadline_assistant.db.session import Database
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    # Экспортированные пользователем боевые ключи не должны включать интеграции в тестах.
    values = Settings.model_construct().model_dump()
    values.update(
        database_url=SecretStr(f"sqlite+aiosqlite:///{tmp_path}/test.db"),
        session_encryption_key=SecretStr(Fernet.generate_key().decode()),
    )
    return Settings(_env_file=None, **values)


@pytest.fixture
def configured(settings: Settings) -> Settings:
    return Settings(
        _env_file=None,
        **{
            **settings.model_dump(),
            "telegram_api_id": 12345,
            "telegram_api_hash": SecretStr("test-api-hash"),
            "llm_api_key": SecretStr("test-api-key"),
            "llm_model": "test-model",
        },
    )


@pytest.fixture
async def database(settings: Settings) -> AsyncIterator[Database]:
    database = Database(settings)
    async with database.engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
        await connection.execute(
            text("CREATE TABLE alembic_version (version_num VARCHAR(32) PRIMARY KEY)")
        )
        await connection.execute(
            text("INSERT INTO alembic_version VALUES (:revision)"), {"revision": SCHEMA_REVISION}
        )
    yield database
    await database.close()


@pytest.fixture
async def users(database: Database, settings: Settings) -> tuple[User, User]:
    async with database.transaction() as session:
        repo = UserRepository(session)
        return await repo.get_or_create(101, settings), await repo.get_or_create(202, settings)


@pytest.fixture
async def connected_users(database: Database, users: tuple[User, User]) -> tuple[User, User]:
    async with database.transaction() as session:
        for user in users:
            await TenantRepository(session, user.id).save_session("encrypted")
            stored = await UserRepository(session).set_study_group(user.id, "БПИ267")
            stored.initial_analysis_choice = True
            stored.initial_analysis_decided_at = stored.created_at
    return users
