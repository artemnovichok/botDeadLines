from datetime import timedelta

from deadline_assistant.config import Settings
from deadline_assistant.db.models import SourceMessage, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import EventData, MessageData, utcnow
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.retention import RetentionService


async def test_shared_source_survives_until_all_events_closed(
    database: Database, users: tuple[User, User], settings: Settings
) -> None:
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        source = await repo.source(
            MessageData(chat_id=-1, message_id=1, text="Два задания", timestamp=utcnow())
        )
        first = await repo.create_event(EventData(title="Первое"))
        second = await repo.create_event(EventData(title="Второе"))
        for event in (first, second):
            await repo.attach_source(event.id, source.id)
        first.status = "completed"
        first.completed_at = utcnow() - timedelta(days=40)
    retention = RetentionService(database, settings)
    assert await retention.cleanup() == 0
    async with database.transaction() as session:
        second = await TenantRepository(session, users[0].id).event(second.id)
        second.status = "completed"
        second.completed_at = utcnow() - timedelta(days=40)
        second.retain_until = utcnow() + timedelta(days=1)
    assert await retention.cleanup() == 0
    async with database.transaction() as session:
        second = await TenantRepository(session, users[0].id).event(second.id)
        second.retain_until = utcnow() - timedelta(seconds=1)
    assert await retention.cleanup() == 1
    async with database.transaction() as session:
        assert (await session.get(SourceMessage, source.id)).original_text is None


async def test_open_conflict_protects_text(
    database: Database, users: tuple[User, User], settings: Settings
) -> None:
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Задание"))
        source = await repo.source(
            MessageData(chat_id=-1, message_id=2, text="Перенос", timestamp=utcnow())
        )
        event.status, event.completed_at = "completed", utcnow() - timedelta(days=40)
        await session.flush()
        conflict = await repo.create_conflict(event.id, EventData(title="Уточнение"), [source.id])
    retention = RetentionService(database, settings)
    assert await retention.cleanup() == 0
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).resolve_conflict(conflict.id, 1)
    assert await retention.cleanup() == 1


async def test_cleanup_respects_user_day_and_resubmission_boundaries(database, users, settings):
    from datetime import datetime

    from sqlalchemy import select

    from deadline_assistant.db.models import Event

    now = datetime.fromisoformat("2026-10-20T00:01:00+03:00")
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        today = await repo.create_event(
            EventData(title="Сегодня", due_at="2026-10-20T00:00:00+03:00", date_precision="day")
        )
        grace = await repo.create_event(
            EventData(
                title="Досдача",
                due_at="2026-08-01T00:00:00+03:00",
                date_precision="day",
                resubmission_end="2026-10-20T00:00:00+03:00",
            )
        )
        unknown = await repo.create_event(EventData(title="Нет даты"))
        old = await repo.create_event(
            EventData(
                title="Просроченное", due_at="2026-08-01T00:00:00+03:00", date_precision="day"
            )
        )
    retention = RetentionService(database, settings)
    await retention.cleanup(now)
    async with database.transaction() as session:
        assert await session.get(Event, today.id)
        assert await session.get(Event, grace.id)
        assert await session.get(Event, unknown.id)
        assert await session.get(Event, old.id) is None
    # Even after the retention period, a deadline with unknown date must survive.
    await retention.cleanup(now + timedelta(days=100))
    async with database.transaction() as session:
        assert await session.get(Event, unknown.id)
        assert await session.get(Event, grace.id) is None
        assert list(await session.scalars(select(Event))) == [await session.get(Event, unknown.id)]


async def test_cleanup_keeps_open_conflict_event_and_all_its_sources(database, users, settings):
    from datetime import datetime

    from deadline_assistant.db.models import Event

    now = datetime.fromisoformat("2026-10-20T12:00:00+03:00")
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(
            EventData(title="Старый спор", due_at="2026-01-01T00:00:00+03:00", date_precision="day")
        )
        source = await repo.source(
            MessageData(chat_id=-1, message_id=1, text="Важный источник", timestamp=now)
        )
        await repo.attach_source(event.id, source.id)
        await repo.create_conflict(event.id, EventData(title="Уточняется"), [source.id])
    await RetentionService(database, settings).cleanup(now)
    async with database.transaction() as session:
        assert await session.get(Event, event.id)
        assert (await session.get(SourceMessage, source.id)).original_text == "Важный источник"


async def test_retention_cleanup_rolls_back_as_one_transaction(
    database, users, settings, monkeypatch
):
    from datetime import datetime

    from sqlalchemy.ext.asyncio import AsyncSession

    from deadline_assistant.db.models import Event

    now = datetime.fromisoformat("2026-10-20T12:00:00+03:00")
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(
                title="Просроченное", due_at="2026-01-01T00:00:00+03:00", date_precision="day"
            )
        )
    execute = AsyncSession.execute

    async def fail_on_text_update(self, statement, *args, **kwargs):
        if str(statement).startswith("UPDATE source_messages"):
            raise RuntimeError("DB failure")
        return await execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", fail_on_text_update)
    import pytest

    with pytest.raises(RuntimeError):
        await RetentionService(database, settings).cleanup(now)
    async with database.transaction() as session:
        assert await session.get(Event, event.id)
