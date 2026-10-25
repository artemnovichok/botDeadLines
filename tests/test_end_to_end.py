import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from telethon import types
from test_bot_menus import bot_runtime, button, click, last_panel, send  # noqa: F401

from deadline_assistant.db.models import ScheduledDelivery, User
from deadline_assistant.domain import (
    AnalysisOutput,
    AnalysisResult,
    ConflictVariant,
    CreateConflict,
    CreateEvent,
    EventData,
    Evidence,
    MessageData,
    MessageRef,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository, event_data
from deadline_assistant.repositories.users import UserRepository


@pytest.fixture
async def connected_student(bot_runtime, database):  # noqa: F811
    runtime, recording = bot_runtime
    await send(runtime, "/start")
    url = last_panel(recording).reply_markup.inline_keyboard[0][0].url
    token = url.split("#token=", 1)[1]
    browser, csrf = await runtime.auth.exchange(token)
    link = await runtime.auth.authenticate(browser, csrf)
    await click(runtime, f"auth:approve:{link.id.hex}")
    link = await runtime.auth.authenticate(browser, csrf)
    client = AsyncMock()
    client.session = SimpleNamespace(save=lambda: "synthetic-session")
    client.get_me.return_value = SimpleNamespace(id=101, bot=False)
    client.send_code_request.return_value = types.auth.SentCode(
        type=types.auth.SentCodeTypeApp(length=5),
        phone_code_hash="synthetic-code-hash",
    )
    runtime.gateway.new_client = MagicMock(return_value=client)
    assert await runtime.login.phone(link, "+79990000000") == "code_required"
    assert await runtime.login.code(link, "12345") == "connected"
    await asyncio.gather(*runtime.login.finalizers)
    await send(runtime, "БПИ267")
    await send(runtime, "/chats")
    await click(runtime, button(last_panel(recording), "Канал 00").callback_data)
    await click(runtime, button(last_panel(recording), "Сохранить").callback_data)
    async with database.transaction() as session:
        user = await UserRepository(session).by_telegram_id(101)
        chat = (await TenantRepository(session, user.id).chats())[0]
    return runtime, recording, user, chat


async def test_complete_student_lifecycle_conflict_restart_and_delete(connected_student, database):
    runtime, recording, user, chat = connected_student
    now = utcnow()
    original = EventData(
        title="Лабораторная по физике",
        due_at=now + timedelta(days=3),
        date_precision="exact",
        confidence=0.95,
    )
    message = MessageData(
        chat_id=chat.telegram_chat_id,
        message_id=1,
        text="Сдать лабораторную через три дня",
        timestamp=now,
    )
    reader = AsyncMock()
    reader.fetch.return_value = [message]
    reader.fetch_sources.return_value = {}
    analyzer = AsyncMock(model="fixture-model")
    analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(
            operations=[
                CreateEvent(
                    event=original,
                    evidence=Evidence(
                        messages=[MessageRef(chat_id=chat.telegram_chat_id, message_id=1)]
                    ),
                )
            ]
        )
    )
    runtime.sync.reader, runtime.sync.analyzer = reader, analyzer
    await click(runtime, "analysis:first")
    await asyncio.gather(*list(runtime.jobs.tasks.values()))
    assert any("Лабораторная по физике" in (getattr(c, "text", "") or "") for c in recording.calls)
    async with database.transaction() as session:
        event = (await TenantRepository(session, user.id).active_events())[0]
    changed = message.model_copy(
        update={"message_id": 2, "text": "Срок спорный: возможно ещё два дня"}
    )
    reader.fetch.return_value = [message, changed]

    async def create_conflict(messages, events, timezone, context):
        current = context.events[0]
        evidence = Evidence(messages=[MessageRef(chat_id=chat.telegram_chat_id, message_id=2)])
        return AnalysisOutput(
            result=AnalysisResult(
                operations=[
                    CreateConflict(
                        event_id=current.id,
                        expected_version=current.version,
                        reason="Противоречащие сроки",
                        evidence=evidence,
                        variants=[
                            ConflictVariant(event=current.data, evidence=evidence),
                            ConflictVariant(
                                event=current.data.model_copy(
                                    update={"due_at": now + timedelta(days=5)}
                                ),
                                evidence=evidence,
                            ),
                        ],
                    )
                ]
            )
        )

    analyzer.analyze.side_effect = create_conflict
    await runtime.cycle.run_user(user.id, send_now=True)
    async with database.transaction() as session:
        repo = TenantRepository(session, user.id)
        conflict = (await repo.conflicts())[0]
        assert (await repo.event(event.id)).due_at == original.due_at
    await send(runtime, f"/resolve {conflict.id} 2")
    async with database.transaction() as session:
        resolved = await TenantRepository(session, user.id).event(event.id)
        assert event_data(resolved).due_at == now + timedelta(days=5)
    calls = analyzer.analyze.await_count
    await runtime.cycle.run_user(user.id, send_now=True)
    assert analyzer.analyze.await_count == calls
    # A daytime restart waits for the next night, preserving checkpoint and delivery state.
    from deadline_assistant.services.scheduler import SchedulerService

    restored = SchedulerService(
        database, runtime.settings, runtime.sync, runtime.scheduler.notifier, runtime.retention
    )
    await restored.tick(now.replace(hour=6, minute=0))
    await restored.tick(now.replace(hour=6, minute=1))
    assert analyzer.analyze.await_count == calls
    from zoneinfo import ZoneInfo

    night = (now + timedelta(days=1)).astimezone(ZoneInfo(user.timezone)).replace(hour=2, minute=10)
    await restored.tick(night)
    await restored.tick(night.replace(minute=11))
    assert analyzer.analyze.await_count == calls
    async with database.transaction() as session:
        assert any(
            row.kind == "sync" and row.status == "sent"
            for row in await session.scalars(select(ScheduledDelivery))
        )
    # Confirmation is mandatory and cannot be supplied by another student.
    await send(runtime, "/delete_data")
    deletion = button(last_panel(recording), "Удалить все").callback_data
    async with database.transaction() as session:
        assert await session.get(User, user.id) is not None
    await click(runtime, deletion, user_id=202)
    async with database.transaction() as session:
        assert await session.get(User, user.id) is not None
    runtime.gateway.revoke = AsyncMock(return_value=True)
    await click(runtime, deletion)
    async with database.transaction() as session:
        assert await session.get(User, user.id) is None
