from collections.abc import AsyncGenerator
from itertools import count
from typing import Any
from unittest.mock import AsyncMock

import pytest
from aiogram.client.session.base import BaseSession
from aiogram.methods import (
    AnswerCallbackQuery,
    EditMessageReplyMarkup,
    EditMessageText,
    GetMe,
    SendMessage,
)
from aiogram.types import CallbackQuery, Chat, InlineKeyboardMarkup, Message, Update
from aiogram.types import User as TelegramUser
from pydantic import SecretStr

from deadline_assistant.config import Settings
from deadline_assistant.domain import EventData, utcnow
from deadline_assistant.handlers.menus import MenuStates
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.runtime import Runtime

IDS = count(1)


class RecordingSession(BaseSession):
    def __init__(self):
        super().__init__()
        self.calls = []

    async def close(self):
        pass

    async def stream_content(self, *args, **kwargs) -> AsyncGenerator[bytes, None]:
        yield b""

    async def make_request(self, bot, method, timeout=None) -> Any:  # noqa: ASYNC109
        self.calls.append(method)
        if isinstance(method, AnswerCallbackQuery):
            return True
        if isinstance(method, GetMe):
            return TelegramUser(
                id=999, is_bot=True, first_name="Assistant", username="test_deadlines_bot"
            )
        if isinstance(method, (SendMessage, EditMessageText, EditMessageReplyMarkup)):
            markup = method.reply_markup
            return Message(
                message_id=getattr(method, "message_id", None) or next(IDS),
                date=utcnow(),
                chat=Chat(id=int(method.chat_id), type="private"),
                from_user=TelegramUser(id=999, is_bot=True, first_name="Assistant"),
                text=getattr(method, "text", "") or "",
                reply_markup=markup if isinstance(markup, InlineKeyboardMarkup) else None,
            )
        raise AssertionError(f"Unexpected bot request: {type(method).__name__}")


def last_panel(recording):
    return next(
        m for m in reversed(recording.calls) if isinstance(m, (SendMessage, EditMessageText))
    )


def button(panel, label):
    return next(b for row in panel.reply_markup.inline_keyboard for b in row if label in b.text)


async def send(runtime, text, user_id=101, chat_type="private", reply_to=None):
    await runtime.dispatcher.feed_update(
        runtime.bot,
        Update(
            update_id=next(IDS),
            message=Message(
                message_id=next(IDS),
                date=utcnow(),
                text=text,
                reply_to_message=Message(
                    message_id=reply_to, date=utcnow(), chat=Chat(id=user_id, type="private")
                )
                if reply_to
                else None,
                chat=Chat(id=user_id if chat_type == "private" else -123, type=chat_type),
                from_user=TelegramUser(id=user_id, is_bot=False, first_name="User"),
            ),
        ),
    )


async def click(runtime, data, user_id=101, message_id=None, callback_id=None):
    await runtime.dispatcher.feed_update(
        runtime.bot,
        Update(
            update_id=next(IDS),
            callback_query=CallbackQuery(
                id=callback_id or str(next(IDS)),
                chat_instance="test",
                data=data,
                from_user=TelegramUser(id=user_id, is_bot=False, first_name="User"),
                message=Message(
                    message_id=message_id or next(IDS),
                    date=utcnow(),
                    text="Bot panel",
                    chat=Chat(id=user_id, type="private"),
                    from_user=TelegramUser(id=999, is_bot=True, first_name="Assistant"),
                ),
            ),
        ),
    )


@pytest.fixture
async def bot_runtime(database, configured):
    configured = Settings(
        _env_file=None,
        **{
            **configured.model_dump(),
            "bot_token": SecretStr("123456:TESTTOKEN"),
            "public_base_url": "https://deadline.example",
        },
    )
    runtime = Runtime(configured)
    await runtime.database.close()
    runtime.database = database
    for name in (
        "auth",
        "login",
        "accounts",
        "sync",
        "jobs",
        "scheduler",
        "retention",
        "corrections",
    ):
        getattr(runtime, name).database = database
    await runtime.bot.session.close()
    recording = RecordingSession()
    runtime.bot.session = recording
    runtime.scheduler.notifier.database = database
    runtime.gateway.dialogs = AsyncMock(
        return_value=[
            {
                "id": -1000000000100 - i,
                "title": f"Канал {i:02}",
                "metadata": {
                    "kind": "channel",
                    "id": 100 + i,
                    "access_hash": 555,
                    "username": "",
                },
            }
            for i in range(12)
        ]
    )
    yield runtime, recording
    await runtime.jobs.close()
    await runtime.login.close()
    await runtime.dispatcher.storage.close()
    await runtime.dispatcher.fsm.events_isolation.close()
    await runtime.analyzer.close()
    await runtime.bot.session.close()


@pytest.fixture
async def connected_bot_runtime(bot_runtime, database, users):
    async with database.transaction() as session:
        for user in users:
            await TenantRepository(session, user.id).save_session("encrypted")
    return bot_runtime


async def test_start_private_keyboard_and_secure_connect_url(bot_runtime):
    runtime, recording = bot_runtime
    await send(runtime, "/start", chat_type="group")
    assert recording.calls == []
    await send(runtime, "/start")
    messages = [m for m in recording.calls if isinstance(m, SendMessage)]
    assert len(messages) == 2
    keyboard = messages[0].reply_markup
    assert keyboard.is_persistent
    assert {b.text for row in keyboard.keyboard for b in row} == {
        "Подключить аккаунт",
        "Настройки",
        "Помощь",
    }
    assert "БПИ267" in messages[1].text
    await send(runtime, "БПИ267")
    assert any(
        isinstance(call, SendMessage) and "сохранена: БПИ267" in call.text
        for call in recording.calls
    )
    url = last_panel(recording).reply_markup.inline_keyboard[0][0].url
    assert "/connect#token=" in url and "user_id" not in url


@pytest.mark.parametrize(
    "base_url", ["https://localhost:8443", "https://127.0.0.1:8443", "https://[::1]:8443"]
)
async def test_local_connect_returns_copyable_link_without_url_button(bot_runtime, base_url):
    from html import unescape

    runtime, recording = bot_runtime
    runtime.settings.public_base_url = base_url
    await send(runtime, "/connect")
    panel = last_panel(recording)
    assert panel.reply_markup is None
    assert panel.parse_mode == "HTML"
    assert "на Mac" in panel.text
    url = unescape(panel.text.split("<code>")[1].split("</code>")[0])
    assert url.startswith(base_url + "/connect#token=")
    browser, csrf = await runtime.auth.exchange(url.split("#token=")[1])
    assert await runtime.auth.authenticate(browser, csrf)


@pytest.mark.parametrize(
    "command",
    [
        "Мои дедлайны",
        "Мои чаты",
        "Редактировать дедлайн",
        "Обновить сводку",
        "Посмотреть источники",
        "/events",
        "/events@test_deadlines_bot",
        "/refresh",
        "/chats",
        "/tracked",
        "/sync",
        "/done invalid",
        "/correct invalid текст",
        "/undo invalid",
        "/sources invalid",
        "/conflicts",
        "/resolve invalid 1",
        "/track -1001",
        "/untrack invalid",
    ],
)
async def test_unconnected_account_cannot_access_deadlines(bot_runtime, database, users, command):
    from sqlalchemy import select

    from deadline_assistant.db.models import EventList

    runtime, recording = bot_runtime
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).create_event(
            EventData(title="Сохранённая работа")
        )
    await send(runtime, command)
    panel = last_panel(recording)
    assert panel.text == "Сначала подключите аккаунт: /connect"
    assert {b.text for row in panel.reply_markup.keyboard for b in row} == {
        "Подключить аккаунт",
        "Настройки",
        "Помощь",
    }
    assert not runtime.jobs.tasks
    runtime.gateway.dialogs.assert_not_awaited()
    async with database.transaction() as session:
        assert await session.scalar(select(EventList)) is None


@pytest.mark.parametrize("status", ["revoked", "disconnected"])
async def test_inactive_session_blocks_old_actions(connected_bot_runtime, database, users, status):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Работа"))
    await send(runtime, "Мои дедлайны")
    await click(runtime, button(last_panel(recording), "Завершить задание").callback_data)
    action = button(last_panel(recording), "Работа").callback_data
    async with database.transaction() as session:
        (await TenantRepository(session, users[0].id).telegram_session()).status = status
    recording.calls.clear()
    await click(runtime, action)
    assert last_panel(recording).text == "Сначала подключите аккаунт: /connect"
    assert any(isinstance(c, AnswerCallbackQuery) and c.show_alert for c in recording.calls)
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).event(event.id)).status == "active"
    recording.calls.clear()
    await send(runtime, "/events")
    assert last_panel(recording).text == "Сначала подключите аккаунт: /connect"


async def test_onboarding_settings_cancel_and_connect_stay_available(bot_runtime):
    runtime, recording = bot_runtime
    await send(runtime, "Настройки")
    assert "не подключён" in last_panel(recording).text
    await click(runtime, button(last_panel(recording), "Часовой пояс").callback_data)
    await send(runtime, "Asia/Yekaterinburg")
    assert "Asia/Yekaterinburg" in last_panel(recording).text
    await send(runtime, "/cancel")
    assert last_panel(recording).reply_markup.keyboard[0][0].text == "Подключить аккаунт"
    await send(runtime, "Подключить аккаунт")
    assert "/connect#token=" in last_panel(recording).reply_markup.inline_keyboard[0][0].url


async def test_chat_picker_pagination_search_save_and_isolation(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("encrypted")
    await send(runtime, "Мои чаты")
    panel = last_panel(recording)
    assert "страница 1/2" in panel.text
    toggle = button(panel, "Канал 00").callback_data
    await click(runtime, toggle)
    assert "✅ Канал 00" in button(last_panel(recording), "Канал 00").text
    async with database.transaction() as session:
        assert await TenantRepository(session, users[0].id).chats() == []
    await click(runtime, toggle, user_id=202)
    assert any(
        isinstance(c, AnswerCallbackQuery) and "устарело" in (c.text or "") for c in recording.calls
    )
    await click(runtime, button(last_panel(recording), "→").callback_data)
    assert "страница 2/2" in last_panel(recording).text
    await click(runtime, button(last_panel(recording), "Поиск").callback_data)
    await send(runtime, "Канал 10")
    panel = last_panel(recording)
    assert "Поиск: Канал 10" in panel.text
    assert any("Канал 10" in b.text for row in panel.reply_markup.inline_keyboard for b in row)
    await click(runtime, button(panel, "Сохранить").callback_data)
    assert "Чатов подключено: 1" in last_panel(recording).text
    assert button(last_panel(recording), "Первый анализ").callback_data == "analysis:first"
    async with database.transaction() as session:
        chats = await TenantRepository(session, users[0].id).chats()
        assert [c.telegram_chat_id for c in chats] == [-1000000000100]
        assert chats[0].peer_metadata["access_hash"] == 555
        assert await TenantRepository(session, users[1].id).chats() == []


async def test_picker_cancel_does_not_apply_changes(connected_bot_runtime, database, users):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("encrypted")
    await send(runtime, "/chats")
    await click(runtime, button(last_panel(recording), "Канал 00").callback_data)
    await click(runtime, button(last_panel(recording), "Отмена").callback_data)
    async with database.transaction() as session:
        assert await TenantRepository(session, users[0].id).chats() == []


async def test_settings_fsm_and_actual_deadline_edit(connected_bot_runtime, database, users):
    runtime, recording = connected_bot_runtime
    await send(runtime, "Настройки")
    await click(runtime, button(last_panel(recording), "Часовой пояс").callback_data)
    await send(runtime, "Asia/Yekaterinburg")
    assert "Asia/Yekaterinburg" in last_panel(recording).text
    await click(runtime, button(last_panel(recording), "Время сводки").callback_data)
    await send(runtime, "09:35")
    assert "09:35" in last_panel(recording).text
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(EventData(title="Работа"))
    await send(runtime, "Редактировать дедлайн")
    await click(runtime, button(last_panel(recording), "Работа").callback_data)
    await click(runtime, button(last_panel(recording), "Срок").callback_data)
    await send(runtime, "25.10.2026 13:30")
    assert "25 октября 2026" in last_panel(recording).text
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).event(event.id)
        assert event.due_at.hour == 8 and event.due_at.minute == 30
        assert event.date_precision == "exact"


async def test_bot_approval_cannot_be_used_by_another_user(connected_bot_runtime, users):
    runtime, recording = connected_bot_runtime
    browser, csrf = await runtime.auth.exchange(await runtime.auth.issue(users[0].id))
    panel = last_panel(recording)
    approve = button(panel, "Это я").callback_data
    await click(runtime, approve, user_id=202)
    assert (await runtime.auth.authenticate(browser, csrf)).approved_at is None
    await click(runtime, approve)
    assert (await runtime.auth.authenticate(browser, csrf)).approved_at is not None


async def test_first_analysis_button_queues_real_service(connected_bot_runtime, database, users):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.save_session("encrypted")
        await repo.track(-1001, "Группа", {"kind": "chat", "id": 1})
    runtime.sync.reader = AsyncMock()
    runtime.sync.reader.fetch.return_value = []
    await click(runtime, "analysis:first")
    await asyncio_gather(runtime.jobs.tasks.values())
    run = await runtime.jobs.latest(users[0].id)
    assert run.status == "succeeded" and run.kind == "initial"
    assert run.chat_count == 1 and run.message_count == 0
    assert any(isinstance(c, SendMessage) and "успешно завершён" in c.text for c in recording.calls)


async def asyncio_gather(tasks):
    import asyncio

    await asyncio.gather(*list(tasks))


async def test_expired_edit_callback_cannot_modify_another_users_event(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Только владельцу")
        )
        await TenantRepository(session, users[1].id).create_event(EventData(title="Чужое"))
    await send(runtime, "Редактировать дедлайн", user_id=202)
    await click(runtime, "edit:pick:" + event.id.hex, user_id=202)
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=202, user_id=202)
    assert await context.get_state() == MenuStates.choosing_event.state
    async with database.transaction() as session:
        assert (
            await TenantRepository(session, users[0].id).event(event.id)
        ).title == "Только владельцу"


async def test_free_text_edit_number_is_uuid_and_applies_immediately(
    connected_bot_runtime, database, users
):
    from deadline_assistant.domain import AnalysisOutput, AnalysisResult, Evidence, UpdateEvent

    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Алгебра")
        )

    async def analyze(messages, events, timezone, context):
        correction = next(c for c in context.corrections if c.id == context.focus_correction_id)
        memory = next(e for e in context.events if e.id == event.id)
        return AnalysisOutput(
            result=AnalysisResult(
                operations=[
                    UpdateEvent(
                        event_id=event.id,
                        expected_version=memory.version,
                        reason="Исправление пользователя",
                        event=EventData(
                            title="Алгебра",
                            due_at="2026-11-12T00:00:00+03:00",
                            date_precision="day",
                            confidence=0.95,
                        ),
                        evidence=Evidence(correction_ids=[correction.id]),
                    )
                ]
            )
        )

    runtime.corrections.analyzer = AsyncMock(model="mock")
    runtime.corrections.analyzer.analyze.side_effect = analyze
    await send(runtime, "Редактировать дедлайн")
    assert button(last_panel(recording), "Алгебра").text.startswith("1.")
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=101, user_id=101)
    prompt = (await context.get_data())["number_prompt"]
    await send(runtime, "1", reply_to=prompt)
    await send(runtime, "Контрольная по алгебре будет 12 ноября, а не 7-го")
    assert "12 ноября 2026" in last_panel(recording).text
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).event(event.id)).due_at.day == 11
    runtime.corrections.analyzer.analyze.assert_awaited_once()


async def test_conflict_buttons_apply_third_option_without_llm(
    connected_bot_runtime, database, users
):
    from deadline_assistant.domain import MessageData

    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Алгебра"))
        source = await repo.source(
            MessageData(chat_id=-1, message_id=1, text="Перенос", timestamp=utcnow())
        )
        conflict = await repo.create_conflict(
            event.id,
            variants=[
                (
                    EventData(
                        title="Алгебра",
                        due_at=f"2026-11-{day}T00:00:00+03:00",
                        date_precision="day",
                    ),
                    [source.id],
                    [],
                    "",
                )
                for day in (12, 14)
            ],
        )
    runtime.corrections.analyzer = AsyncMock()
    await send(runtime, "/conflicts")
    third = button(last_panel(recording), "Вариант 3").callback_data
    await click(runtime, third, user_id=202)
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).conflict(conflict.id)).status == "open"
    await click(runtime, third)
    assert "14 ноября 2026" in last_panel(recording).text
    runtime.corrections.analyzer.analyze.assert_not_awaited()


def test_telegram_chunks_preserve_text_with_emoji_within_utf16_limit():
    from deadline_assistant.handlers.bot import telegram_chunks

    text = "Итоги 🎓" * 1000
    parts = telegram_chunks(text)
    assert "".join(parts) == text
    assert all(len(part.encode("utf-16-le")) // 2 <= 3500 for part in parts)


async def test_old_number_and_old_list_buttons_cannot_select_new_event(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        first = await repo.create_event(EventData(title="Первое"))
        second = await repo.create_event(EventData(title="Второе"))
    await send(runtime, "Редактировать дедлайн")
    old_button = button(last_panel(recording), "Первое").callback_data
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=101, user_id=101)
    old_prompt = (await context.get_data())["number_prompt"]
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).complete(first.id)
    await send(runtime, "Редактировать дедлайн")
    await send(runtime, "1", reply_to=old_prompt)
    assert await context.get_state() == MenuStates.choosing_event.state
    await click(runtime, old_button)
    assert await context.get_state() == MenuStates.choosing_event.state
    assert "event_id" not in await context.get_data()
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).event(second.id)).status == "active"


async def test_done_button_uses_uuid_and_is_one_shot(connected_bot_runtime, database, users):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Завершить")
        )
    await send(runtime, "Мои дедлайны")
    action = button(last_panel(recording), "Завершить задание").callback_data
    await click(runtime, action, user_id=202)
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).event(event.id)).status == "active"
    await click(runtime, action)
    target = button(last_panel(recording), "Завершить").callback_data
    assert len(target.encode()) <= 64
    await click(runtime, target, message_id=10000)
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=101, user_id=101)
    assert await context.get_state() is None
    before = len([c for c in recording.calls if isinstance(c, SendMessage)])
    await click(runtime, target, message_id=10000)
    assert len([c for c in recording.calls if isinstance(c, SendMessage)]) == before
    assert "уже обработано" in recording.calls[-1].text
    async with database.transaction() as session:
        saved = await TenantRepository(session, users[0].id).event(event.id)
        assert saved.status == "completed" and saved.completed_by_user
    assert "Завершить</b>" not in last_panel(recording).text


async def test_event_version_change_rejects_correction_in_progress(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        item = await repo.create_event(EventData(title="Работа"))
    await send(runtime, "Редактировать дедлайн")
    await click(runtime, button(last_panel(recording), "Работа").callback_data)
    runtime.corrections.analyzer = AsyncMock()
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.event(item.id)
        from deadline_assistant.repositories.tenant import event_data

        await repo.write_event(
            event, event_data(event).model_copy(update={"title": "Новая работа"})
        )
    await send(runtime, "Перенеси на завтра")
    runtime.corrections.analyzer.analyze.assert_not_awaited()
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).event(item.id)).title == "Новая работа"


async def test_undo_correction_button_restores_previous_data_without_llm(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Старое название"))
        correction = await repo.correction(event.id, "Новое название")
        await repo.apply_correction(correction.id, EventData(title="Новое название"))
    runtime.corrections.analyzer = AsyncMock()
    await send(runtime, "Мои дедлайны")
    await click(runtime, button(last_panel(recording), "Отменить своё исправление").callback_data)
    await click(runtime, button(last_panel(recording), "Новое название").callback_data)
    assert "Старое название" in last_panel(recording).text
    runtime.corrections.analyzer.analyze.assert_not_awaited()
    async with database.transaction() as session:
        assert (
            await TenantRepository(session, users[0].id).correction_by_id(correction.id)
        ).status == "reverted"


async def test_sources_show_author_time_and_one_full_text_at_a_time(
    connected_bot_runtime, database, users
):
    from deadline_assistant.domain import MessageData

    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Работа"))
        for number in (1, 2):
            source = await repo.source(
                MessageData(
                    chat_id=-1,
                    message_id=number,
                    text=f"Полный текст номер {number}",
                    timestamp=utcnow(),
                    author_name="Преподаватель",
                    source_url=f"https://t.me/math/{number}",
                )
            )
            await repo.attach_source(event.id, source.id)
    await send(runtime, "Посмотреть источники")
    await click(runtime, button(last_panel(recording), "Работа").callback_data)
    text = "\n".join(c.text for c in recording.calls if isinstance(c, SendMessage))
    assert "Полный текст номер 1" in text and "Полный текст номер 2" not in text
    assert "Преподаватель" in text and "Отправлено:" in text and "https://t.me/math/1" in text
    await click(runtime, button(last_panel(recording), "Следующий").callback_data, user_id=202)
    text = "\n".join(c.text for c in recording.calls if isinstance(c, SendMessage))
    assert "Полный текст номер 2" not in text
    await click(runtime, button(last_panel(recording), "Следующий").callback_data)
    text = "\n".join(c.text for c in recording.calls if isinstance(c, SendMessage))
    assert "Полный текст номер 2" in text


async def test_refresh_help_status_and_cancel_do_not_call_llm(connected_bot_runtime):
    runtime, recording = connected_bot_runtime
    runtime.corrections.analyzer = AsyncMock()
    runtime.sync.analyzer = AsyncMock()
    await send(runtime, "Обновить сводку")
    assert "УЧЕБНЫЕ ДЕДЛАЙНЫ" in last_panel(recording).text
    await click(runtime, button(last_panel(recording), "Обновить сводку").callback_data)
    await click(runtime, button(last_panel(recording), "Помощь").callback_data)
    assert "/status" in last_panel(recording).text
    await send(runtime, "/status")
    assert "Анализ ещё не запускался" in last_panel(recording).text
    await send(runtime, "Настройки")
    await click(runtime, button(last_panel(recording), "Время сводки").callback_data)
    await send(runtime, "/cancel")
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=101, user_id=101)
    assert await context.get_state() is None
    runtime.corrections.analyzer.analyze.assert_not_awaited()
    runtime.sync.analyzer.analyze.assert_not_awaited()


async def test_failed_correction_is_saved_and_shows_last_summary(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(EventData(title="Работа"))
    runtime.corrections.analyzer = AsyncMock()
    runtime.corrections.analyzer.analyze.side_effect = TimeoutError()
    await send(runtime, "Редактировать дедлайн")
    await click(runtime, button(last_panel(recording), "Работа").callback_data)
    await send(runtime, "Будет 20 ноября")
    assert "Работа" in last_panel(recording).text
    assert "не обработанное исправление" in last_panel(recording).text
    from sqlalchemy import select

    from deadline_assistant.db.models import UserCorrection

    async with database.transaction() as session:
        correction = await session.scalar(select(UserCorrection))
        assert correction.status == "pending" and correction.event_id == event.id


async def test_fast_double_toggle_and_redelivered_callback_do_not_undo_selection(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("encrypted")
    await send(runtime, "Мои чаты")
    toggle = button(last_panel(recording), "Канал 00").callback_data
    await click(runtime, toggle, message_id=12345, callback_id="same-update")
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=101, user_id=101)
    selected = (await context.get_data())["selected"]
    await click(runtime, toggle, message_id=12345, callback_id="same-update")
    await click(runtime, toggle, message_id=12345, callback_id="second-fast-tap")
    assert selected == (await context.get_data())["selected"]
    assert len(selected) == 1


async def test_old_edit_button_cannot_turn_into_completion_action(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        item = await TenantRepository(session, users[0].id).create_event(EventData(title="Работа"))
    await send(runtime, "Мои дедлайны")
    summary_panel = last_panel(recording)
    await click(runtime, button(summary_panel, "Редактировать дедлайн").callback_data)
    old_pick = button(last_panel(recording), "Работа").callback_data
    await click(runtime, button(summary_panel, "Завершить задание").callback_data)
    await click(runtime, old_pick)
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).event(item.id)).status == "active"


async def test_actual_telegram_forbidden_marks_block_and_inbound_message_clears_it(
    connected_bot_runtime, database, users
):
    from aiogram.exceptions import TelegramForbiddenError
    from aiogram.methods import SendMessage

    from deadline_assistant.db.models import User
    from deadline_assistant.domain import DeliveryBlocked

    runtime, recording = connected_bot_runtime
    notifier = runtime.scheduler.notifier
    original = recording.make_request

    async def forbidden(bot, method, timeout=None):  # noqa: ASYNC109
        raise TelegramForbiddenError(method=SendMessage(chat_id=101, text="x"), message="Forbidden")

    recording.make_request = forbidden
    with pytest.raises(DeliveryBlocked):
        await notifier.send(101, "Сводка")
    async with database.transaction() as session:
        assert (await session.get(User, users[0].id)).bot_blocked_at
    recording.make_request = original
    await send(runtime, "/help")
    async with database.transaction() as session:
        assert (await session.get(User, users[0].id)).bot_blocked_at is None


async def test_undo_list_includes_accidentally_expired_and_completed_work(
    connected_bot_runtime, database, users
):
    runtime, recording = connected_bot_runtime
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Восстановить срок"))
        correction = await repo.correction(event.id, "Ошибочная прошедшая дата")
        await repo.apply_correction(
            correction.id,
            EventData(
                title="Восстановить срок", due_at="2020-01-01T00:00:00+03:00", date_precision="day"
            ),
        )
        completed = await repo.create_event(EventData(title="Выполненное"))
        correction2 = await repo.correction(completed.id, "Другое название")
        await repo.apply_correction(correction2.id, EventData(title="Исправленное выполненное"))
        await repo.complete(completed.id)
    await send(runtime, "Мои дедлайны")
    assert "Восстановить срок" not in last_panel(recording).text
    await click(runtime, button(last_panel(recording), "Отменить своё исправление").callback_data)
    undo_panel = last_panel(recording)
    assert button(undo_panel, "Восстановить срок")
    assert button(undo_panel, "Исправленное выполненное")
    await click(runtime, button(undo_panel, "Восстановить срок").callback_data)
    assert "Восстановить срок" in last_panel(recording).text
    async with database.transaction() as session:
        restored = await TenantRepository(session, users[0].id).event(event.id)
        assert restored.due_at is None
        assert (await TenantRepository(session, users[0].id).event(completed.id)).completed_by_user


async def test_group_prompt_once_survives_fsm_restart_and_settings_allow_change(
    bot_runtime, database, users
):
    from deadline_assistant.repositories.users import UserRepository

    runtime, recording = bot_runtime
    await send(runtime, "/start")
    assert "БПИ267" in last_panel(recording).text
    context = runtime.dispatcher.fsm.get_context(bot=runtime.bot, chat_id=101, user_id=101)
    await context.clear()
    await send(runtime, "бпи 267")
    assert "сохранена: БПИ267" in last_panel(recording).text
    async with database.transaction() as session:
        repo = UserRepository(session)
        assert (await repo.require(users[0].id)).study_group == "БПИ267"
        assert (await repo.require(users[1].id)).study_group is None
    recording.calls.clear()
    await send(runtime, "/start")
    assert not any(
        isinstance(call, SendMessage) and "Укажите вашу учебную группу" in call.text
        for call in recording.calls
    )
    await send(runtime, "/settings")
    assert "Учебная группа: БПИ267" in last_panel(recording).text
    await click(runtime, button(last_panel(recording), "Учебная группа").callback_data)
    await send(runtime, "группа неизвестна")
    assert "например БПИ267" in last_panel(recording).text
    await send(runtime, "БПИ268")
    assert "сохранена: БПИ268" in last_panel(recording).text
    await send(runtime, "/group БПИ269")
    assert "сохранена: БПИ269" in last_panel(recording).text


async def test_skipping_group_does_not_prompt_again(bot_runtime):
    runtime, recording = bot_runtime
    await send(runtime, "/start")
    await send(runtime, "/cancel")
    assert "/connect#token=" in last_panel(recording).reply_markup.inline_keyboard[0][0].url
    recording.calls.clear()
    await send(runtime, "/start")
    assert not any(
        isinstance(call, SendMessage) and "Укажите вашу учебную группу" in call.text
        for call in recording.calls
    )
