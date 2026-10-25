from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import func, select, text
from sqlalchemy.exc import IntegrityError

from deadline_assistant.config import Settings
from deadline_assistant.db.models import Event, EventSource, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import EntityNotFound, EventData, MessageData, utcnow
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository


async def test_user_creation_is_idempotent(database: Database, settings: Settings) -> None:
    async with database.transaction() as session:
        repo = UserRepository(session)
        first = await repo.get_or_create(777, settings)
        second = await repo.get_or_create(777, settings)
        assert first.id == second.id
        assert await session.scalar(select(func.count()).select_from(User)) == 1


async def test_tenant_isolation(database: Database, users: tuple[User, User]) -> None:
    one, two = users
    async with database.transaction() as session:
        owner = TenantRepository(session, one.id)
        event = await owner.create_event(EventData(title="Курсовая"))
        chat = await owner.track(-10001, "Группа", {"kind": "chat", "id": 1})
        source = await owner.source(
            MessageData(chat_id=-10001, message_id=1, text="Сдать курсовую", timestamp=utcnow())
        )
        await owner.attach_source(event.id, source.id)
        await owner.save_session("зашифрованная сессия")
        other = TenantRepository(session, two.id)
        assert await other.active_events() == []
        assert await other.chats() == []
        assert await other.telegram_session() is None
        for operation in [
            other.event(event.id),
            other.complete(event.id),
            other.correction(event.id, "Исправление"),
            other.sources(event.id),
            other.chat(chat.id),
            other.attach_source(event.id, source.id),
        ]:
            with pytest.raises(EntityNotFound):
                await operation
        with pytest.raises(EntityNotFound):
            await other.resolve_conflict(uuid4(), 1)
        own_event = await other.create_event(EventData(title="Экзамен"))
        with pytest.raises(EntityNotFound):
            await other.attach_source(own_event.id, source.id)


async def test_database_rejects_cross_tenant_source_link(
    database: Database, users: tuple[User, User]
) -> None:
    async with database.transaction() as session:
        one, two = (TenantRepository(session, user.id) for user in users)
        event = await one.create_event(EventData(title="Лабораторная"))
        source = await two.source(
            MessageData(chat_id=-1, message_id=1, text="Текст", timestamp=utcnow())
        )
    with pytest.raises(IntegrityError):
        async with database.transaction() as session:
            session.add(EventSource(event_id=event.id, source_id=source.id, user_id=users[0].id))
            await session.flush()
    async with database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(EventSource)) == 0


async def test_transaction_rollback(database: Database, users: tuple[User, User]) -> None:
    with pytest.raises(RuntimeError):
        async with database.transaction() as session:
            await TenantRepository(session, users[0].id).create_event(EventData(title="Откат"))
            raise RuntimeError("Ошибка после записи")
    async with database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 0


async def test_outer_rollback_includes_nested_user_creation(
    database: Database, settings: Settings
) -> None:
    with pytest.raises(RuntimeError):
        async with database.transaction() as session:
            await UserRepository(session).get_or_create(999, settings)
            raise RuntimeError("Откат после SAVEPOINT")
    async with database.transaction() as session:
        assert await UserRepository(session).by_telegram_id(999) is None


async def test_processed_messages_scope_and_edit(
    database: Database, users: tuple[User, User]
) -> None:
    message = MessageData(chat_id=-1, message_id=123, text="Задание", timestamp=utcnow())
    async with database.transaction() as session:
        first, second = (TenantRepository(session, user.id) for user in users)
        await first.mark_processed(message)
        await session.flush()
        assert await first.is_processed(message)
        assert not await second.is_processed(message)
        assert not await first.is_processed(message.model_copy(update={"chat_id": -2}))
        changed = message.model_copy(update={"text": "Перенос", "edit_timestamp": utcnow()})
        assert not await first.is_processed(changed)
        await first.mark_processed(changed)
        await session.flush()
        assert await first.is_processed(changed)


async def test_conflict_resolution_checks_owner(
    database: Database, users: tuple[User, User]
) -> None:
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Задание"))
        source = await repo.source(
            MessageData(chat_id=-1, message_id=1, text="Перенос", timestamp=utcnow())
        )
        conflict = await repo.create_conflict(
            event.id, EventData(title="Новое название"), [source.id]
        )
        await session.flush()
        with pytest.raises(EntityNotFound):
            await TenantRepository(session, users[1].id).resolve_conflict(conflict.id, 2)
        assert event.title == "Задание"
        await repo.resolve_conflict(conflict.id, 2)
        assert event.title == "Новое название"
        assert conflict.status == "resolved"
        await session.flush()
        assert [s.id for s in await repo.sources(event.id)] == [source.id]


async def test_utc_roundtrip_and_sqlite_pragmas(
    database: Database, users: tuple[User, User]
) -> None:
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Тест", due_at="2026-10-20T10:00:00+03:00", date_precision="exact")
        )
    async with database.transaction() as session:
        saved = await TenantRepository(session, users[0].id).event(event.id)
        assert saved.due_at == datetime(2026, 10, 20, 7, tzinfo=UTC)
        assert saved.created_at.tzinfo is UTC
        assert await session.scalar(text("PRAGMA foreign_keys")) == 1
        assert await session.scalar(text("PRAGMA journal_mode")) == "wal"
