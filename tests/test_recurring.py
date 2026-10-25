from datetime import UTC, date, datetime, time, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, func, select

from deadline_assistant.db.models import Event, EventSource, RecurringDeadline, SourceMessage, User
from deadline_assistant.domain import EntityNotFound, EventData, MessageData, StaleState
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.analysis import MemoryBuilder
from deadline_assistant.services.digest import build_digest
from deadline_assistant.services.recurring import (
    MSK,
    RecurringService,
    next_due,
    resolve_first_due,
)

NOW = datetime(2026, 10, 20, 9, tzinfo=UTC)  # Tuesday, 12:00 MSK.
FIRST = datetime(2026, 10, 26, 15, tzinfo=UTC)  # Next Monday, 18:00 MSK.


def test_first_deadline_weekday_date_and_time_modes():
    assert resolve_first_due(0, time(18), MSK, now=NOW) == FIRST
    assert resolve_first_due(1, time(13), MSK, now=NOW) == NOW + timedelta(hours=1)
    assert resolve_first_due(1, time(11), MSK, now=NOW) == datetime(2026, 10, 27, 8, tzinfo=UTC)
    # No time always means the selected date at 23:59 Moscow, even for another user zone.
    assert resolve_first_due(date(2026, 10, 20), None, "Asia/Tokyo", now=NOW) == datetime(
        2026, 10, 20, 20, 59, tzinfo=UTC
    )
    assert resolve_first_due(date(2026, 10, 20), time(19), "Asia/Tokyo", now=NOW) == datetime(
        2026, 10, 20, 10, tzinfo=UTC
    )
    with pytest.raises(ValueError):
        resolve_first_due(date(2026, 10, 19), time(18), MSK, now=NOW)
    with pytest.raises(ValueError):
        resolve_first_due(7, time(18), MSK, now=NOW)


@pytest.mark.parametrize("interval", [1, 7, 14])
def test_repeat_stays_anchored_and_skips_missed_occurrences(interval):
    assert next_due(FIRST, interval, MSK, NOW) == FIRST
    assert next_due(FIRST, interval, MSK, FIRST) == FIRST
    assert next_due(FIRST, interval, MSK, FIRST + timedelta(seconds=1)) == FIRST + timedelta(
        days=interval
    )
    later = FIRST + timedelta(days=1000, hours=1)
    expected = FIRST + timedelta(days=(1000 // interval + 1) * interval)
    assert next_due(FIRST, interval, MSK, later) == expected


def test_repeat_preserves_local_time_across_dst():
    first = datetime.fromisoformat("2026-10-19T18:00:00+02:00")
    due = next_due(first, 7, "Europe/Berlin", NOW)
    assert due == datetime(2026, 10, 26, 17, tzinfo=UTC)


async def test_recurring_lifecycle_digest_close_repeat_and_stop(database, users):
    series_id = uuid4()
    async with database.transaction() as session:
        service = RecurringService(session, users[0].id)
        series = await service.create(
            "Матанализ <ДЗ>", 7, FIRST, MSK, True, series_id=series_id, now=NOW
        )
        duplicate = await service.create(
            "Матанализ <ДЗ>", 7, FIRST, MSK, True, series_id=series_id, now=NOW
        )
        assert duplicate.id == series.id
        assert len(await service.list_active()) == 1
        current = await service.current(series.id, now=NOW)
        assert current.due_at == FIRST and current.status == "active"
        assert current.recurring_deadline_id == series.id
        assert (await service.current(series.id, now=NOW)).id == current.id
        repo = TenantRepository(session, users[0].id)
        assert await repo.active_events() == []  # Manual series never enter LLM context.
        assert (await MemoryBuilder().build(repo, [], MSK, now=NOW)).events == []
        assert (
            await repo.duplicate(
                EventData(title=series.title, due_at=FIRST, date_precision="exact")
            )
            is None
        )
        with pytest.raises(ValueError, match="Циклические дедлайны"):
            await repo.correction(current.id, "Перенести на завтра")
        _, before = await build_digest(repo, MSK, now=NOW)
        assert "Матанализ &lt;ДЗ&gt;" in before and "<s>" not in before
        closed = await service.close(current.id, current.version, now=NOW)
        assert closed.status == "completed"
        _, after = await build_digest(repo, MSK, now=NOW)
        assert "<s>" in after and "Матанализ &lt;ДЗ&gt;" in after
        assert (await service.current(series.id, now=FIRST)).id == current.id
        current_id, current_version = current.id, current.version

    async with database.transaction() as session:
        service = RecurringService(session, users[0].id)
        later = FIRST + timedelta(days=35, seconds=1)
        upcoming = await service.current(series_id, now=later)
        assert upcoming.id != current_id
        assert upcoming.due_at == FIRST + timedelta(days=42)
        assert upcoming.status == "active"
        assert (await service.current_events(now=later))[0].id == upcoming.id
        with pytest.raises(StaleState):
            await service.close(current_id, current_version, now=later)
        events = list(await session.scalars(select(Event).where(Event.user_id == users[0].id)))
        assert len(events) == 2  # No copies for five missed weeks and no read duplicates.
        repo = TenantRepository(session, users[0].id)
        _, summary = await build_digest(repo, MSK, now=later)
        assert summary.count("Матанализ &lt;ДЗ&gt;") == 1 and "<s>" not in summary
        series = await service.get(series_id)
        await service.stop(series_id, series.version)
        assert await service.list_active() == []
        assert await service.current_events(now=later) == []
        _, stopped = await build_digest(repo, MSK, now=later)
        assert "Матанализ" not in stopped
        assert await service.current_events(now=later + timedelta(days=100)) == []


async def test_recurring_isolation_versions_and_schedule_edit(database, users):
    async with database.transaction() as session:
        owner = RecurringService(session, users[0].id)
        other = RecurringService(session, users[1].id)
        series = await owner.create("Алгебра", 7, FIRST, MSK, True, now=NOW)
        current = await owner.current(series.id, now=NOW)
        version = series.version
        assert await other.list_active() == []
        with pytest.raises(EntityNotFound):
            await other.get(series.id)
        with pytest.raises(EntityNotFound):
            await other.current(series.id, now=NOW)
        with pytest.raises(EntityNotFound):
            await other.close(current.id, current.version, now=NOW)
        with pytest.raises(EntityNotFound):
            await other.stop(series.id, version)
        with pytest.raises(StaleState):
            await owner.close(current.id, current.version + 1, now=NOW)
        changed = await owner.update(
            series.id,
            version,
            title="Алгебра: новая группа",
            interval_days=14,
            first_due_at=FIRST + timedelta(days=1),
            timezone=MSK,
            has_time=True,
            now=NOW,
        )
        assert changed.version > version
        replacement = await owner.current(series.id, now=NOW)
        assert replacement.id != current.id and replacement.title == "Алгебра: новая группа"
        assert replacement.due_at == FIRST + timedelta(days=1)
        with pytest.raises(StaleState):
            await owner.stop(series.id, version)
        with pytest.raises(StaleState):
            await owner.close(current.id, current.version, now=NOW)


@pytest.mark.parametrize("interval", [0, 15])
async def test_invalid_repeat_interval_does_not_create_series(database, users, interval):
    async with database.transaction() as session:
        service = RecurringService(session, users[0].id)
        with pytest.raises(ValueError):
            await service.create("Неверный интервал", interval, FIRST, MSK, True, now=NOW)
        assert await service.list_active() == []


async def test_user_purge_removes_series_occurrences_and_ordinary_sources(database, users):
    owner_id, other_id = (user.id for user in users)
    async with database.transaction() as session:
        service = RecurringService(session, owner_id)
        series = await service.create("Повторяемое ДЗ", 7, FIRST, MSK, True, now=NOW)
        await service.current(series.id, now=NOW)
        repo = TenantRepository(session, owner_id)
        ordinary = await repo.create_event(EventData(title="Разовое ДЗ"))
        source = await repo.source(
            MessageData(chat_id=-1001, message_id=1, text="Разовое ДЗ", timestamp=NOW)
        )
        await repo.attach_source(ordinary.id, source.id)
        other = RecurringService(session, other_id)
        other_series = await other.create("ДЗ другого пользователя", 7, FIRST, MSK, True, now=NOW)
        other_event = await other.current(other_series.id, now=NOW)

    async with database.transaction() as session:
        await session.execute(delete(User).where(User.id == owner_id))

    async with database.transaction() as session:
        assert await session.get(User, owner_id) is None
        for model in (RecurringDeadline, Event, SourceMessage, EventSource):
            count = await session.scalar(
                select(func.count()).select_from(model).where(model.user_id == owner_id)
            )
            assert count == 0
        assert await session.get(User, other_id) is not None
        assert await session.get(RecurringDeadline, other_series.id) is not None
        assert await session.get(Event, other_event.id) is not None
