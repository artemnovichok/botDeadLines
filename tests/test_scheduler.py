from datetime import UTC, datetime
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from deadline_assistant.config import Settings
from deadline_assistant.db.models import ScheduledDelivery, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import UserPreferences
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.retention import RetentionService
from deadline_assistant.services.scheduler import SchedulerService


@pytest.mark.parametrize("status", [None, "revoked", "disconnected"])
async def test_scheduler_never_sends_to_unconnected_accounts(database, users, settings, status):
    from deadline_assistant.repositories.tenant import TenantRepository

    if status:
        async with database.transaction() as session:
            for user in users:
                credential = await TenantRepository(session, user.id).save_session("encrypted")
                credential.status = status
    notifier = AsyncMock()
    sync = AsyncMock()
    scheduler = SchedulerService(
        database, settings, sync, notifier, RetentionService(database, settings)
    )
    now = datetime(2026, 10, 20, 8, 34, tzinfo=UTC)
    await scheduler.tick(now)
    await scheduler.tick(now)
    await scheduler._deliver(users[0], "digest", now)
    notifier.send.assert_not_awaited()
    notifier.send_conflict.assert_not_awaited()
    sync.sync_user.assert_not_awaited()
    async with database.transaction() as session:
        assert await session.scalar(select(ScheduledDelivery)) is None


async def test_only_connected_user_receives_digest(database, users, settings):
    from deadline_assistant.repositories.tenant import TenantRepository

    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("encrypted")
        user = await UserRepository(session).set_study_group(users[0].id, "БПИ267")
        user.initial_analysis_choice = False
    notifier = AsyncMock()
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    await scheduler.tick(datetime(2026, 10, 20, 6, tzinfo=UTC))
    notifier.send.assert_awaited_once()
    assert notifier.send.await_args.args[0] == users[0].telegram_user_id


async def test_pending_delivery_is_not_resumed_after_disconnect(
    database, connected_users, settings
):
    from datetime import timedelta

    from deadline_assistant.domain import IntegrationUnavailable
    from deadline_assistant.repositories.tenant import TenantRepository
    from deadline_assistant.services.delivery import SummaryDelivery

    now = datetime(2026, 10, 20, 6, tzinfo=UTC)
    async with database.transaction() as session:
        repo = TenantRepository(session, connected_users[0].id)
        (await repo.telegram_session()).status = "revoked"
        delivery = ScheduledDelivery(
            user_id=connected_users[0].id,
            kind="digest",
            local_date=now.date(),
            next_attempt_at=now,
            payload=[{"kind": "html", "text": "Старые дедлайны"}],
        )
        session.add(delivery)
        await session.flush()
        delivery_id = delivery.id
    notifier = AsyncMock()
    with pytest.raises(IntegrationUnavailable, match="/connect"):
        await SummaryDelivery(database, notifier).deliver(connected_users[0].id, delivery_id)
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    await scheduler._deliver(connected_users[0], "digest", now + timedelta(minutes=5))
    notifier.send.assert_not_awaited()


async def test_disconnect_stops_remaining_delivery_parts(database, connected_users):
    from deadline_assistant.domain import IntegrationUnavailable
    from deadline_assistant.repositories.tenant import TenantRepository
    from deadline_assistant.services.delivery import SummaryDelivery

    async with database.transaction() as session:
        delivery = ScheduledDelivery(
            user_id=connected_users[0].id,
            kind="digest",
            local_date=datetime(2026, 10, 20).date(),
            payload=[{"kind": "html", "text": "Часть 1"}, {"kind": "html", "text": "Часть 2"}],
        )
        session.add(delivery)
        await session.flush()
        delivery_id = delivery.id

    async def disconnect(*args):
        async with database.transaction() as session:
            credential = await TenantRepository(session, connected_users[0].id).telegram_session()
            credential.status = "revoked"

    notifier = AsyncMock()
    notifier.send.side_effect = disconnect
    with pytest.raises(IntegrationUnavailable, match="/connect"):
        await SummaryDelivery(database, notifier).deliver(connected_users[0].id, delivery_id)
    notifier.send.assert_awaited_once()


async def test_manual_summary_requires_connection(database, users):
    from deadline_assistant.domain import IntegrationUnavailable
    from deadline_assistant.services.delivery import SummaryDelivery

    notifier = AsyncMock()
    with pytest.raises(IntegrationUnavailable, match="/connect"):
        await SummaryDelivery(database, notifier).send_current(users[0].id)
    notifier.send.assert_not_awaited()
    async with database.transaction() as session:
        assert await session.scalar(select(ScheduledDelivery)) is None


async def test_digest_local_time_and_restart_idempotence(
    database: Database, connected_users: tuple[User, User], settings: Settings
) -> None:
    async with database.transaction() as session:
        await UserRepository(session).preferences(
            connected_users[1].id,
            UserPreferences(timezone="America/Los_Angeles", preferred_digest_time="08:00"),
        )
    notifier = AsyncMock()
    now = datetime(
        2026, 10, 19, 5, 30, tzinfo=UTC
    )  # Москва 08:30, Лос-Анджелес 22:30 предыдущего дня
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    await scheduler.tick(now)
    assert (
        notifier.send.await_count == 1
    )  # позднюю сводку LA пропускаем, чтобы не присылать её ночью
    restored = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    await restored.tick(now)
    assert notifier.send.await_count == 1
    async with database.transaction() as session:
        rows = list(await session.scalars(select(ScheduledDelivery)))
        dates = {row.user_id: row.local_date.isoformat() for row in rows}
        assert dates[connected_users[0].id] == "2026-10-19"
        assert dates[connected_users[1].id] == "2026-10-18"
        assert next(row for row in rows if row.user_id == connected_users[1].id).status == "skipped"


async def test_delivery_retry_survives_failure(
    database: Database, connected_users: tuple[User, User], settings: Settings
) -> None:
    notifier = AsyncMock()
    notifier.send.side_effect = RuntimeError("Временная ошибка")
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    now = datetime(2026, 10, 19, 6, tzinfo=UTC)
    await scheduler.tick(now)
    notifier.send.side_effect = None
    await scheduler.tick(now)  # ещё рано для повтора
    assert notifier.send.await_count == 2
    await scheduler.tick(datetime(2026, 10, 19, 7, tzinfo=UTC))
    assert notifier.send.await_count == 4


async def test_partial_delivery_resumes_without_resending_successful_parts(
    database, connected_users, settings
):
    from datetime import timedelta

    from deadline_assistant.domain import EventData

    now = datetime(2026, 10, 20, 6, tzinfo=UTC)
    async with database.transaction() as session:
        for number in range(40):
            await TenantRepository(session, connected_users[0].id).create_event(
                EventData(
                    title=f"Работа {number} " + "Условия" * 30,
                    due_at="2026-10-21T12:00:00+03:00",
                    date_precision="exact",
                )
            )
    notifier = AsyncMock()
    notifier.send.side_effect = [None, RuntimeError("Временная ошибка")]
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    await scheduler._deliver(connected_users[0], "digest", now)
    async with database.transaction() as session:
        delivery = await session.scalar(select(ScheduledDelivery))
        assert delivery.next_part == 1 and len(delivery.payload) > 2
        first_payload = delivery.payload[0]["text"]
    notifier.send.reset_mock(side_effect=True)
    restored = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    await restored._deliver(connected_users[0], "digest", now + timedelta(minutes=5))
    assert all(call.args[1] != first_payload for call in notifier.send.await_args_list)
    async with database.transaction() as session:
        delivery = await session.scalar(select(ScheduledDelivery))
        assert delivery.status == "sent" and delivery.next_part == len(delivery.payload)


async def test_blocked_user_stops_delivery_until_next_user_message(
    database, connected_users, settings
):
    from datetime import timedelta

    from deadline_assistant.domain import DeliveryBlocked

    notifier = AsyncMock()
    notifier.send.side_effect = DeliveryBlocked()
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    now = datetime(2026, 10, 20, 6, tzinfo=UTC)
    await scheduler.tick(now)
    assert notifier.send.await_count == 2
    async with database.transaction() as session:
        assert (await session.get(User, connected_users[0].id)).bot_blocked_at
        assert all(
            row.status == "blocked" for row in await session.scalars(select(ScheduledDelivery))
        )
    await scheduler.tick(now + timedelta(days=1))
    assert notifier.send.await_count == 2


async def test_retry_after_and_attempt_limit_prevent_spam(database, connected_users, settings):
    from datetime import timedelta

    from aiogram.exceptions import TelegramRetryAfter
    from aiogram.methods import SendMessage

    settings.delivery_max_attempts = 2
    notifier = AsyncMock()
    notifier.send.side_effect = TelegramRetryAfter(
        method=SendMessage(chat_id=101, text="x"), message="Flood", retry_after=3600
    )
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    now = datetime(2026, 10, 20, 6, tzinfo=UTC)
    await scheduler._deliver(connected_users[0], "digest", now)
    await scheduler._deliver(connected_users[0], "digest", now + timedelta(minutes=30))
    assert notifier.send.await_count == 1
    await scheduler._deliver(connected_users[0], "digest", now + timedelta(hours=1))
    await scheduler._deliver(connected_users[0], "digest", now + timedelta(hours=2))
    assert notifier.send.await_count == 2
    async with database.transaction() as session:
        assert (await session.scalar(select(ScheduledDelivery))).status == "failed"


async def test_night_failure_keeps_checkpoint_and_morning_digest(
    database, connected_users, configured
):
    from deadline_assistant.domain import EventData, MessageData
    from deadline_assistant.services.sync import SyncService

    async with database.transaction() as session:
        repo = TenantRepository(session, connected_users[0].id)
        await repo.save_session("encrypted")
        chat = await repo.track(-1001, "Группа", {"kind": "chat", "id": 1})
        await repo.create_event(EventData(title="Последний достоверный дедлайн"))
    reader = AsyncMock()
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=55, text="Новое задание", timestamp=datetime.now(UTC))
    ]
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = TimeoutError()
    sync = SyncService(database, reader, analyzer, configured)
    notifier = AsyncMock()
    scheduler = SchedulerService(
        database, configured, sync, notifier, RetentionService(database, configured)
    )
    await scheduler.tick(datetime.fromisoformat("2026-10-20T02:10:00+03:00"))
    await scheduler.tick(datetime(2026, 10, 20, 6, tzinfo=UTC))
    summary = next(call.args[1] for call in notifier.send.await_args_list if call.args[0] == 101)
    assert "Последний достоверный дедлайн" in summary and "устаревшими" in summary
    async with database.transaction() as session:
        assert (
            await TenantRepository(session, connected_users[0].id).chat(chat.id)
        ).last_processed_message_id == 0


async def test_empty_night_still_sends_morning_without_llm(database, connected_users, configured):
    from deadline_assistant.services.sync import SyncService

    async with database.transaction() as session:
        repo = TenantRepository(session, connected_users[0].id)
        await repo.save_session("encrypted")
        await repo.track(-1001, "Группа", {"kind": "chat", "id": 1})
    reader = AsyncMock()
    reader.fetch.return_value = []
    analyzer = AsyncMock(model="mock")
    notifier = AsyncMock()
    scheduler = SchedulerService(
        database,
        configured,
        SyncService(database, reader, analyzer, configured),
        notifier,
        RetentionService(database, configured),
    )
    await scheduler.tick(datetime(2026, 10, 20, 6, tzinfo=UTC))
    analyzer.analyze.assert_not_awaited()
    assert any(
        call.args[0] == 101 and "УЧЕБНЫЕ ДЕДЛАЙНЫ" in call.args[1]
        for call in notifier.send.await_args_list
    )


async def test_sync_only_runs_in_night_window_once_per_local_date(
    database, connected_users, configured
):

    async with database.transaction() as session:
        repo = TenantRepository(session, connected_users[0].id)
        await repo.save_session("encrypted")
        await repo.track(-1001, "Группа", {"kind": "chat", "id": 1})
    sync = AsyncMock()
    scheduler = SchedulerService(
        database, configured, sync, AsyncMock(), RetentionService(database, configured)
    )
    now = datetime.fromisoformat("2026-10-21T01:00:00+03:00")
    await scheduler.tick(now)
    sync.sync_user.assert_not_awaited()
    await scheduler.tick(datetime.fromisoformat("2026-10-21T14:00:00+03:00"))
    sync.sync_user.assert_not_awaited()
    now = datetime.fromisoformat("2026-10-22T02:10:00+03:00")
    await scheduler.tick(now)
    await scheduler.tick(now)
    sync.sync_user.assert_awaited_once_with(connected_users[0].id)
    async with database.transaction() as session:
        row = await session.scalar(
            select(ScheduledDelivery).where(ScheduledDelivery.kind == "sync")
        )
        assert row.local_date.isoformat() == "2026-10-22" and row.status == "sent"


@pytest.mark.parametrize("group,choice", [(None, True), ("БПИ267", None)])
async def test_schedule_waits_for_onboarding(database, users, configured, group, choice):
    async with database.transaction() as session:
        user = await UserRepository(session).require(users[0].id)
        user.study_group, user.initial_analysis_choice = group, choice
        repo = TenantRepository(session, user.id)
        await repo.save_session("encrypted")
        await repo.track(-1001, "Группа", {"kind": "chat", "id": 1})
    sync, notifier = AsyncMock(), AsyncMock()
    scheduler = SchedulerService(
        database, configured, sync, notifier, RetentionService(database, configured)
    )
    await scheduler.tick(datetime.fromisoformat("2026-10-21T02:10:00+03:00"))
    await scheduler.tick(datetime.fromisoformat("2026-10-21T08:00:00+03:00"))
    sync.sync_user.assert_not_awaited()
    notifier.send.assert_not_awaited()
