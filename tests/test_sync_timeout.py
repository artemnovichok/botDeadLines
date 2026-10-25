import asyncio
import importlib
from datetime import timedelta
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select
from test_sync import prepare

from deadline_assistant.db.models import AnalysisRun, ProcessedMessage
from deadline_assistant.domain import (
    AnalysisIdleTimeout,
    AnalysisOutput,
    AnalysisResult,
    EventData,
    InputTooLarge,
    MessageData,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.jobs import render_run
from deadline_assistant.services.sync import SyncService


@pytest.fixture(autouse=True)
def loaded_analysis_protocol():
    # Runtime loads the analyzer at startup, before a sync deadline is started.
    importlib.import_module("deadline_assistant.integrations.llm")


@pytest.mark.parametrize("split", [False, True])
async def test_long_sync_renews_deadline_after_each_committed_batch(
    database, users, configured, split
):
    await prepare(database, users[0])
    # Fractions shorten this regression; deployment settings are still positive integers.
    configured = configured.model_copy(
        update={"sync_timeout_seconds": 0.5, "sync_batch_size": 50 if split else 1}
    )
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=i, text="Учёба", timestamp=utcnow())
        for i in range(1, 6)
    ]
    handled = []

    async def analyze(messages, *args):
        if len(messages) > 1:
            raise InputTooLarge()
        await asyncio.sleep(0.2)
        handled.append(messages[0].message_id)
        return AnalysisOutput(result=AnalysisResult())

    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = analyze
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    assert handled == [1, 2, 3, 4, 5]
    async with database.transaction() as session:
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "succeeded" and run.duration_seconds > 0.5
        assert run.new_message_count == 5
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 5


async def test_idle_timeout_keeps_committed_progress_and_resumes_without_duplicates(
    database, users, configured
):
    await prepare(database, users[0])
    configured = configured.model_copy(update={"sync_timeout_seconds": 0.5, "sync_batch_size": 1})
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=i, text="Учёба", timestamp=utcnow()) for i in (1, 2)
    ]
    handled = []
    stall = True

    async def analyze(messages, *args):
        handled.append(messages[0].message_id)
        if stall and messages[0].message_id == 2:
            await asyncio.Event().wait()
        return AnalysisOutput(result=AnalysisResult())

    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = analyze
    service = SyncService(database, reader, analyzer, configured)
    with pytest.raises(AnalysisIdleTimeout):
        await service.sync_user(users[0].id)
    async with database.transaction() as session:
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "failed" and run.error == "AnalysisIdleTimeout"
        assert run.new_message_count == 1 and run.error_count == 1
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 1
    stall = False
    await service.sync_user(users[0].id)
    assert handled == [1, 2, 2]
    async with database.transaction() as session:
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 2
        assert await session.scalar(select(func.count()).select_from(ProcessedMessage)) == 2


async def test_long_source_refresh_renews_deadline(database, users, configured):
    chat = await prepare(database, users[0])
    configured = configured.model_copy(update={"sync_timeout_seconds": 0.5, "sync_batch_size": 1})
    old_messages = [
        MessageData(
            chat_id=-1001,
            message_id=i,
            text="Исходный текст",
            timestamp=utcnow() - timedelta(days=365),
        )
        for i in range(1, 5)
    ]
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        (await repo.chat(chat.id)).last_processed_message_id = 100
        for message in old_messages:
            event = await repo.create_event(EventData(title=f"Работа {message.message_id}"))
            source = await repo.source(message)
            await repo.attach_source(event.id, source.id)
    reader = AsyncMock()
    reader.fetch.return_value = []
    reader.fetch_sources.return_value = {
        m.message_id: m.model_copy(update={"text": "Изменённый текст"}) for m in old_messages
    }

    async def analyze(*args):
        await asyncio.sleep(0.2)
        return AnalysisOutput(result=AnalysisResult())

    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = analyze
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    assert analyzer.analyze.await_count == 4
    async with database.transaction() as session:
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "succeeded" and run.duration_seconds > 0.5
        assert (
            await TenantRepository(session, users[0].id).chat(chat.id)
        ).last_processed_message_id == 100


async def test_pending_corrections_renew_deadline(database, users, configured):
    await prepare(database, users[0])
    configured = configured.model_copy(update={"sync_timeout_seconds": 0.5})
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        for number in range(4):
            event = await repo.create_event(EventData(title=f"Работа {number}"))
            await repo.correction(event.id, "Уточнить условия")
    reader = AsyncMock()
    reader.fetch.return_value = []
    reader.fetch_sources.return_value = {}

    async def analyze(*args):
        await asyncio.sleep(0.2)
        return AnalysisOutput(result=AnalysisResult())

    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = analyze
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    assert analyzer.analyze.await_count == 4
    async with database.transaction() as session:
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "succeeded" and run.duration_seconds > 0.5


def test_running_status_shows_elapsed_duration_and_timeout_is_readable():
    run = AnalysisRun(
        status="running",
        started_at=utcnow() - timedelta(seconds=75),
        chat_count=13,
        message_count=990,
        new_message_count=900,
        event_count=5,
        conflict_count=0,
        error_count=0,
        duration_seconds=0,
    )
    text = render_run(run)
    assert "Длительность: 75." in text
    run.status = "failed"
    run.error = "AnalysisIdleTimeout"
    assert "текущая порция прервана" in render_run(run)
    run.error = "TimeoutError"
    assert "Истекло время ожидания ответа Telegram или ИИ" in render_run(run)
