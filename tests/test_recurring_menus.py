from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock, patch

from aiogram.methods import AnswerCallbackQuery, EditMessageReplyMarkup, SendMessage
from sqlalchemy import select
from test_bot_menus import button, click, last_panel, send

from deadline_assistant.db.models import Event, RecurringDeadline
from deadline_assistant.domain import utcnow
from deadline_assistant.handlers.recurring import RecurringStates
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.recurring import MSK, RecurringService

pytest_plugins = ("test_bot_menus",)


async def start_creation(runtime, recording, title="Матанализ"):
    await click(runtime, "menu:recurring")
    await click(runtime, button(last_panel(recording), "Добавить дедлайн").callback_data)
    await send(runtime, title)


async def test_weekday_creation_no_time_uses_moscow_and_escapes_title(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    runtime.sync.analyzer = AsyncMock()
    runtime.corrections.analyzer = AsyncMock()
    async with database.transaction() as session:
        (await UserRepository(session).require(users[0].id)).timezone = "Asia/Yekaterinburg"
    now = datetime(2026, 10, 27, 9, tzinfo=UTC)  # Tuesday.
    with (
        patch("deadline_assistant.services.recurring.utcnow", return_value=now),
        patch("deadline_assistant.handlers.recurring.utcnow", return_value=now),
    ):
        await start_creation(runtime, recording, "Матанализ <1> & зачёт")
        await click(runtime, button(last_panel(recording), "7").callback_data)
        await click(runtime, button(last_panel(recording), "Понедельник").callback_data)
        await click(runtime, button(last_panel(recording), "Без времени").callback_data)
        preview = last_panel(recording)
        assert "02.11.2026 23:59 · МСК" in preview.text
        assert "&lt;1&gt; &amp;" in preview.text
        save = button(preview, "Сохранить").callback_data
        await click(runtime, save)
        assert "02.11.2026 23:59 · МСК" in last_panel(recording).text
        await click(runtime, save)  # A repeated old save cannot create another series.
    async with database.transaction() as session:
        series = list(await session.scalars(select(RecurringDeadline)))
        assert len(series) == 1
        assert series[0].title == "Матанализ <1> & зачёт"
        assert series[0].interval_days == 7
        assert series[0].timezone == MSK and not series[0].has_time
        assert series[0].first_due_at == datetime(2026, 11, 2, 20, 59, tzinfo=UTC)
    runtime.sync.analyzer.analyze.assert_not_awaited()
    runtime.corrections.analyzer.analyze.assert_not_awaited()


async def test_close_strikes_only_occurrence_then_stop_disables_series(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        series = await RecurringService(session, users[0].id).create(
            "Цикл по алгебре", 7, utcnow() + timedelta(days=2), MSK, True
        )
    await click(runtime, "menu:recurring")
    await click(runtime, button(last_panel(recording), "Цикл по алгебре").callback_data)
    close = button(last_panel(recording), "Закрыть").callback_data
    await click(runtime, close, user_id=202)
    assert any(
        isinstance(call, AnswerCallbackQuery) and call.show_alert for call in recording.calls
    )
    await click(runtime, close)
    assert "<s>Цикл по алгебре</s>" in last_panel(recording).text
    assert button(last_panel(recording), "Закрыто ✓")
    assert any(
        isinstance(call, SendMessage) and "УЧЕБНЫЕ ДЕДЛАЙНЫ" in call.text and "<s>" in call.text
        for call in recording.calls
    )
    async with database.transaction() as session:
        stored = await RecurringService(session, users[0].id).get(series.id)
        event = await RecurringService(session, users[0].id).current(series.id)
        assert stored.enabled and event.status == "completed"
    await send(runtime, "Мои дедлайны")
    await click(runtime, button(last_panel(recording), "Редактировать дедлайн").callback_data)
    await click(runtime, button(last_panel(recording), "Цикл по алгебре").callback_data)
    assert "<s>Цикл по алгебре</s>" in last_panel(recording).text
    assert button(last_panel(recording), "Закрыто ✓")
    await click(runtime, button(last_panel(recording), "Закончить отслеживание").callback_data)
    cancelled_confirmation = button(last_panel(recording), "Закончить отслеживание").callback_data
    before_cancel = len(recording.calls)
    await click(runtime, button(last_panel(recording), "Отмена").callback_data)
    assert any(
        isinstance(call, EditMessageReplyMarkup) and call.reply_markup is None
        for call in recording.calls[before_cancel:]
    )
    await click(runtime, cancelled_confirmation)
    async with database.transaction() as session:
        assert (await RecurringService(session, users[0].id).get(series.id)).enabled
    await click(runtime, button(last_panel(recording), "Закончить отслеживание").callback_data)
    await click(runtime, button(last_panel(recording), "Закончить отслеживание").callback_data)
    async with database.transaction() as session:
        assert not (await RecurringService(session, users[0].id).get(series.id)).enabled
    recording.calls.clear()
    await send(runtime, "Мои дедлайны")
    assert "Цикл по алгебре" not in last_panel(recording).text


async def test_back_cancel_invalid_values_and_old_step_do_not_save(connected_bot_runtime, database):
    runtime, recording = connected_bot_runtime
    await start_creation(runtime, recording)
    previous_interval = button(last_panel(recording), "7").callback_data
    await send(runtime, "15")
    assert "Периодичность" in last_panel(recording).text
    await click(runtime, button(last_panel(recording), "14").callback_data)
    await click(runtime, previous_interval)
    assert any(
        isinstance(call, AnswerCallbackQuery) and call.show_alert for call in recording.calls
    )
    await click(runtime, button(last_panel(recording), "Назад").callback_data)
    await click(runtime, button(last_panel(recording), "1").callback_data)
    await click(runtime, button(last_panel(recording), "Понедельник").callback_data)
    await send(runtime, "99:99")
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=101, user_id=101)
    assert await context.get_state() == RecurringStates.time.state
    assert button(last_panel(recording), "Отмена")
    await click(runtime, button(last_panel(recording), "Отмена").callback_data)
    assert await context.get_state() is None
    async with database.transaction() as session:
        assert await session.scalar(select(RecurringDeadline)) is None


async def test_explicit_date_time_edit_and_stale_close_are_versioned(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    now = datetime(2026, 10, 27, 9, tzinfo=UTC)
    with (
        patch("deadline_assistant.services.recurring.utcnow", return_value=now),
        patch("deadline_assistant.handlers.recurring.utcnow", return_value=now),
    ):
        await start_creation(runtime, recording, "Проверка практики")
        await click(runtime, button(last_panel(recording), "10").callback_data)
        await click(runtime, button(last_panel(recording), "Указать дату").callback_data)
        await send(runtime, "02.11.2026")
        await send(runtime, "18:30")
        assert "02.11.2026 18:30 · МСК" in last_panel(recording).text
        async with database.transaction() as session:
            (await UserRepository(session).require(users[0].id)).timezone = "Asia/Yekaterinburg"
        await click(runtime, button(last_panel(recording), "Сохранить").callback_data)
        assert "02.11.2026 18:30 · МСК" in last_panel(recording).text
        async with database.transaction() as session:
            series = await session.scalar(select(RecurringDeadline))
            assert series.timezone == MSK
            assert series.first_due_at == datetime(2026, 11, 2, 15, 30, tzinfo=UTC)
            (await UserRepository(session).require(users[0].id)).timezone = MSK
        old_close = button(last_panel(recording), "Закрыть").callback_data
        await click(runtime, button(last_panel(recording), "Изменить").callback_data)
        await send(runtime, "Новое название")
        await click(runtime, button(last_panel(recording), "3").callback_data)
        await send(runtime, "04.11.2026")
        await send(runtime, "19:00")
        await click(runtime, button(last_panel(recording), "Сохранить").callback_data)
        assert "Новое название" in last_panel(recording).text
        await click(runtime, old_close)
        async with database.transaction() as session:
            series = await session.scalar(select(RecurringDeadline))
            assert series.title == "Новое название"
            assert series.interval_days == 3
            assert series.first_due_at == datetime(2026, 11, 4, 16, tzinfo=UTC)
            event = await RecurringService(session, users[0].id).current(series.id)
            assert event.status == "active"
    assert any(
        isinstance(call, AnswerCallbackQuery) and call.show_alert for call in recording.calls
    )


async def test_unconnected_or_missing_group_cannot_open_recurring(bot_runtime, database, users):
    runtime, recording = bot_runtime
    await click(runtime, "menu:recurring")
    assert "Сначала подключите аккаунт" in last_panel(recording).text
    from deadline_assistant.repositories.tenant import TenantRepository

    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("encrypted")
    await click(runtime, "rc:new")
    assert "Укажите вашу учебную группу" in last_panel(recording).text
    assert button(last_panel(recording), "Отмена")
    async with database.transaction() as session:
        assert await session.scalar(select(RecurringDeadline)) is None
        assert await session.scalar(select(Event)) is None
