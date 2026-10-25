import asyncio
from unittest.mock import AsyncMock

import pytest
from sqlalchemy import select

from deadline_assistant.db.models import AnalysisRun
from deadline_assistant.domain import AnalysisOutput, AnalysisResult, IntegrationUnavailable
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.jobs import AnalysisJobs, render_run
from deadline_assistant.services.sync import SyncService


async def make_jobs(database, users, configured):
    async with database.transaction() as session:
        await UserRepository(session).set_study_group(users[0].id, "БПИ267")
        repo = TenantRepository(session, users[0].id)
        await repo.save_session("encrypted")
        await repo.track(-1001, "Группа", {"kind": "chat", "id": 1})
    reader = AsyncMock()
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(result=AnalysisResult())
    notifier = AsyncMock()
    sync = SyncService(database, reader, analyzer, configured)
    return AnalysisJobs(database, sync, notifier), reader, notifier


async def test_background_job_deduplicates_and_does_not_block(database, users, configured):
    jobs, reader, notifier = await make_jobs(database, users, configured)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def fetch(*args):
        entered.set()
        await finish.wait()
        return []

    reader.fetch.side_effect = fetch
    assert await jobs.enqueue(users[0].id, initial=True)
    await entered.wait()
    assert not await jobs.enqueue(users[0].id, initial=True)
    assert (await jobs.latest(users[0].id)).status == "running"
    assert await jobs.latest(users[1].id) is None
    finish.set()
    await asyncio.gather(*list(jobs.tasks.values()))
    run = await jobs.latest(users[0].id)
    assert run.status == "succeeded"
    assert "Сообщений собрано: 0" in render_run(run)
    assert notifier.send.await_count == 2  # Status and the current digest.
    assert notifier.send.call_args.args[0] == 101


async def test_restart_recovers_queued_jobs_and_marks_active_interrupted(
    database, users, configured
):
    jobs, reader, notifier = await make_jobs(database, users, configured)
    reader.fetch.return_value = []
    async with database.transaction() as session:
        running = AnalysisRun(user_id=users[1].id, status="running", model="mock")
        session.add(running)
        session.add(AnalysisRun(user_id=users[0].id, status="queued", model="mock", kind="initial"))
        await session.flush()
        running_id = running.id
    await jobs.start()
    await asyncio.gather(*list(jobs.tasks.values()))
    assert (await jobs.latest(users[0].id)).status == "succeeded"
    async with database.transaction() as session:
        assert (await session.get(AnalysisRun, running_id)).status == "interrupted"


async def test_failed_job_reports_failure_and_keeps_status(database, users, configured):
    jobs, reader, notifier = await make_jobs(database, users, configured)
    reader.fetch.side_effect = IntegrationUnavailable("Telegram временно недоступен")
    assert await jobs.enqueue(users[0].id)
    await asyncio.gather(*list(jobs.tasks.values()))
    assert (await jobs.latest(users[0].id)).status == "failed"
    text = notifier.send.call_args.args[1]
    assert "завершился с ошибкой" in text and "Telegram временно недоступен" in text


async def test_first_analysis_requires_chats(database, users, configured):
    jobs, reader, notifier = await make_jobs(database, users, configured)
    with pytest.raises(IntegrationUnavailable):
        await jobs.enqueue(users[1].id, initial=True)
    assert not jobs.tasks
    async with database.transaction() as session:
        assert await session.scalar(select(AnalysisRun)) is None


async def test_invalid_llm_response_has_readable_status_and_does_not_advance_cursor(
    database, users, configured
):
    from deadline_assistant.domain import AnalysisValidationError, MessageData, utcnow

    jobs, reader, notifier = await make_jobs(database, users, configured)
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=1, text="Лабораторная", timestamp=utcnow())
    ]
    jobs.sync.analyzer.analyze.side_effect = AnalysisValidationError("Невалидный ответ ИИ")
    assert await jobs.enqueue(users[0].id, initial=True)
    await asyncio.gather(*list(jobs.tasks.values()))
    run = await jobs.latest(users[0].id)
    assert run.status == "failed" and run.new_message_count == 0
    text = notifier.send.call_args.args[1]
    assert "Ответ ИИ не прошёл проверку формата или источников" in text
    assert "Необработанные сообщения будут прочитаны повторно" in text
    async with database.transaction() as session:
        chats = await TenantRepository(session, users[0].id).chats()
        assert chats[0].last_processed_message_id == 0
