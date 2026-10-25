import asyncio
from unittest.mock import AsyncMock

from sqlalchemy import select
from test_jobs import make_jobs
from test_sync import prepare

from deadline_assistant.db.models import AnalysisRun
from deadline_assistant.domain import AnalysisOutput, AnalysisResult, EventData, MessageData, utcnow
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.analysis import MemoryBuilder
from deadline_assistant.services.sync import SyncService


async def test_catalog_keeps_other_events_without_unrelated_full_sources(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        own = await repo.create_event(EventData(title="Текущая контрольная"))
        other = await repo.create_event(EventData(title="Другая работа", subject="Физика"))
        own_source = await repo.source(
            MessageData(
                chat_id=-1,
                message_id=1,
                timestamp=utcnow(),
                text="Свой источник",
            )
        )
        other_source = await repo.source(
            MessageData(
                chat_id=-2,
                message_id=1,
                timestamp=utcnow(),
                text="Полный источник " * 700,
            )
        )
        await repo.attach_source(own.id, own_source.id)
        await repo.attach_source(other.id, other_source.id)
        message = MessageData(chat_id=-1, message_id=2, timestamp=utcnow(), text="Перенос")
        context = await MemoryBuilder().build(repo, [message], "Europe/Moscow")
        assert {e.id for e in context.events} == {own.id, other.id}
        assert {s.id for s in context.sources} == {own_source.id}
        assert next(e for e in context.events if e.id == other.id).source_ids == []
        related = message.model_copy(update={"text": "Физика: новые условия"})
        context = await MemoryBuilder().build(repo, [related], "Europe/Moscow")
        assert {s.id for s in context.sources} == {own_source.id, other_source.id}
        correction = await repo.correction(other.id, "Изменить условия")
        context = await MemoryBuilder().build(
            repo,
            [],
            "Europe/Moscow",
            focus_correction_id=correction.id,
        )
        assert other_source.id in {s.id for s in context.sources}
        assert (await repo.sources(other.id))[0].original_text == other_source.original_text


async def test_time_slice_commits_result_and_returns_partial(
    database, users, configured, monkeypatch
):
    await prepare(database, users[0])
    configured.sync_batch_size = 1
    configured.sync_max_duration_seconds = 1
    elapsed = [0.0]
    monkeypatch.setattr("deadline_assistant.services.sync.monotonic", lambda: elapsed[0])
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=i, timestamp=utcnow(), text="Учёба") for i in (1, 2)
    ]

    async def analyze(*args):
        elapsed[0] += 2
        return AnalysisOutput(result=AnalysisResult())

    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = analyze
    sync = SyncService(database, reader, analyzer, configured)
    await sync.sync_user(users[0].id)
    assert analyzer.analyze.await_count == 1
    async with database.transaction() as session:
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "partial" and run.error is None and run.error_count == 0
        assert run.new_message_count == 1
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 1 and chat.initial_upper_message_id == 2
    await sync.sync_user(users[0].id)
    assert [call.args[0][0].message_id for call in analyzer.analyze.await_args_list] == [1, 2]


async def test_token_budget_stops_before_next_paid_batch_and_resumes(database, users, configured):
    await prepare(database, users[0])
    configured.sync_batch_size = 1
    configured.sync_max_total_tokens = 30
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=i, timestamp=utcnow(), text="Учёба")
        for i in (1, 2, 3)
    ]
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(),
        token_usage={"total_tokens": 15},
    )
    sync = SyncService(database, reader, analyzer, configured)
    await sync.sync_user(users[0].id)
    assert analyzer.analyze.await_count == 2
    async with database.transaction() as session:
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "partial" and run.new_message_count == 2
        assert run.token_usage["total_tokens"] == 30
    await sync.sync_user(users[0].id)
    assert [call.args[0][0].message_id for call in analyzer.analyze.await_args_list] == [1, 2, 3]


async def test_progress_is_sent_while_request_waits_and_stop_is_owner_scoped(
    database, users, configured
):
    configured.analysis_progress_interval_seconds = 0.02
    jobs, reader, notifier = await make_jobs(database, users, configured)
    progress = asyncio.Event()

    async def receive(user_id, text):
        if "Анализ: выполняется." in text:
            progress.set()

    async def fetch(*args):
        await asyncio.Event().wait()

    notifier.send.side_effect = receive
    reader.fetch.side_effect = fetch
    assert await jobs.enqueue(users[0].id)
    try:
        await asyncio.wait_for(progress.wait(), timeout=2)
        assert not await jobs.stop(users[1].id)
        assert await jobs.stop(users[0].id)
        assert (await jobs.latest(users[0].id)).status == "interrupted"
        assert users[0].id not in jobs.tasks
        jobs.sync.analyzer.analyze.assert_not_awaited()
    finally:
        await jobs.close()


async def test_partial_background_run_sends_status_and_saved_summary(database, users, configured):
    configured.sync_batch_size = 1
    configured.sync_max_total_tokens = 1
    jobs, reader, notifier = await make_jobs(database, users, configured)
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=i, timestamp=utcnow(), text="Учёба") for i in (1, 2)
    ]
    jobs.sync.analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(),
        token_usage={"total_tokens": 1},
    )
    assert await jobs.enqueue(users[0].id)
    await asyncio.gather(*list(jobs.tasks.values()))
    assert (await jobs.latest(users[0].id)).status == "partial"
    texts = [call.args[1] for call in notifier.send.await_args_list]
    assert len(texts) == 2
    assert "Достигнут лимит" in texts[0] and "Ошибка:" not in texts[0]
    assert "Обработана часть истории" in texts[1]
