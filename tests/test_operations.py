import asyncio
import os
import signal
import sys
from datetime import timedelta
from unittest.mock import AsyncMock, patch

import pytest
from sqlalchemy import select

from deadline_assistant.db.base import Base
from deadline_assistant.db.models import AnalysisBatch, AnalysisRun, EventList, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import (
    AnalysisOutput,
    AnalysisResult,
    CreateEvent,
    EntityNotFound,
    EventData,
    Evidence,
    InputTooLarge,
    MessageData,
    MessageRef,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.accounts import AccountService
from deadline_assistant.services.analysis import OperationApplier
from deadline_assistant.services.coordination import JobBusy
from deadline_assistant.services.cycle import CycleService
from deadline_assistant.services.jobs import AnalysisJobs
from deadline_assistant.services.sync import SyncService


async def prepare(database, user, chat_id=-1001):
    async with database.transaction() as session:
        repo = TenantRepository(session, user.id)
        await repo.save_session("encrypted")
        return await repo.track(chat_id, "Учебный чат", {"kind": "chat", "id": abs(chat_id)})


def pipeline(database, settings):
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=1, text="Сдать лабораторную", timestamp=utcnow())
    ]
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(
            operations=[
                CreateEvent(
                    event=EventData(title="Лабораторная"),
                    evidence=Evidence(messages=[MessageRef(chat_id=-1001, message_id=1)]),
                ),
            ]
        ),
        token_usage={"prompt_tokens": 30, "completion_tokens": 10},
    )
    return SyncService(database, reader, analyzer, settings), analyzer


async def dump(database):
    async with database.transaction() as session:
        return {
            table.name: [dict(row) for row in (await session.execute(select(table))).mappings()]
            for table in Base.metadata.sorted_tables
        }


async def test_dry_run_uses_pipeline_without_mutating_any_production_table(
    database, users, configured
):
    await prepare(database, users[0])
    sync, analyzer = pipeline(database, configured)
    notifier = AsyncMock()
    before = await dump(database)
    readonly = Database(configured, readonly=True)
    try:
        service = CycleService(readonly, configured, sync, notifier)
        results = await service.run_many([users[0]], dry_run=True)
        assert results[0].error is None
        assert results[0].events == 1 and results[0].proposals[0]["действие"] == "создать"
    finally:
        await readonly.close()
    assert await dump(database) == before
    notifier.send.assert_not_called()
    analyzer.analyze.assert_awaited_once()


async def test_crash_after_llm_journal_before_checkpoint_reuses_result(database, users, configured):
    await prepare(database, users[0])
    sync, analyzer = pipeline(database, configured)
    with patch.object(OperationApplier, "apply", side_effect=RuntimeError("simulated crash")):
        with pytest.raises(RuntimeError):
            await sync.sync_user(users[0].id)
    async with database.transaction() as session:
        assert await session.scalar(select(AnalysisBatch)) is not None
        repo = TenantRepository(session, users[0].id)
        assert not await repo.active_events()
        assert (await repo.chats())[0].last_processed_message_id == 0
    assert await sync.sync_user(users[0].id) == 1
    assert await sync.sync_user(users[0].id) == 0
    analyzer.analyze.assert_awaited_once()


async def test_two_services_share_user_lock_and_do_not_bill_twice(database, users, configured):
    await prepare(database, users[0])
    sync, analyzer = pipeline(database, configured)
    other = Database(configured)
    second = SyncService(other, sync.reader, analyzer, configured)
    try:
        assert sorted(
            await asyncio.gather(
                sync.sync_user(users[0].id),
                second.sync_user(users[0].id),
            )
        ) == [0, 1]
        analyzer.analyze.assert_awaited_once()
    finally:
        await other.close()


async def test_server_recovery_does_not_interrupt_live_cli(database, users, configured):
    async with database.transaction() as session:
        run = AnalysisRun(user_id=users[0].id, status="running", model="mock")
        session.add(run)
        await session.flush()
        run_id = run.id
    other = Database(configured)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def live_cli():
        async with other.lock(f"analysis:{users[0].id}"):
            entered.set()
            await finish.wait()

    task = asyncio.create_task(live_cli())
    await entered.wait()
    try:
        sync, _ = pipeline(database, configured)
        await AnalysisJobs(database, sync, None).start()
        async with database.transaction() as session:
            assert (await session.get(AnalysisRun, run_id)).status == "running"
    finally:
        finish.set()
        await task
        await other.close()


async def test_context_splitting_keeps_tail_and_checkpoint(database, users, configured):
    await prepare(database, users[0])
    configured.sync_batch_size = 50
    sync, analyzer = pipeline(database, configured)
    messages = [
        MessageData(chat_id=-1001, message_id=i, text=f"Полный текст {i}", timestamp=utcnow())
        for i in range(1, 21)
    ]
    sync.reader.fetch.return_value = messages
    processed = []

    async def analyze(batch, *args):
        if len(batch) > 3:
            raise InputTooLarge()
        processed.extend(batch)
        return AnalysisOutput(result=AnalysisResult())

    analyzer.analyze.side_effect = analyze
    await sync.sync_user(users[0].id)
    assert [(m.message_id, m.text) for m in processed] == [(m.message_id, m.text) for m in messages]
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).chats())[
            0
        ].last_processed_message_id == 20


async def test_failed_user_does_not_delay_others(database, users, configured):
    for user in users:
        await prepare(database, user)
    sync, analyzer = pipeline(database, configured)
    entered, finish = asyncio.Event(), asyncio.Event()

    async def run(user_id):
        if user_id == users[0].id:
            entered.set()
            await finish.wait()
            raise RuntimeError()
        return 0

    sync.sync_user = AsyncMock(side_effect=run)
    completed = []
    cycle = CycleService(database, configured, sync, None)
    task = asyncio.create_task(cycle.run_many(list(users), on_result=completed.append))
    await entered.wait()
    async with asyncio.timeout(3):
        while not completed:  # noqa: ASYNC110
            await asyncio.sleep(0.01)
    assert completed[0].telegram_user_id == 202
    finish.set()
    results = await task
    assert sum(r.error is not None for r in results) == 1


async def test_full_delete_requires_owner_and_removes_all_owned_rows(database, users, configured):
    await prepare(database, users[0])
    sync, _ = pipeline(database, configured)
    await sync.sync_user(users[0].id)
    async with database.transaction() as session:
        confirmation = EventList(
            user_id=users[0].id,
            entries=[{"action": "delete_account"}],
            expires_at=utcnow() + timedelta(minutes=10),
        )
        session.add(confirmation)
        await session.flush()
        confirmation_id = confirmation.id
    gateway = AsyncMock()
    gateway.revoke.return_value = True
    accounts = AccountService(database, gateway)
    with pytest.raises(EntityNotFound):
        await accounts.delete_data(users[1].id, confirmation_id)
    assert await accounts.delete_data(users[0].id, confirmation_id)
    async with database.transaction() as session:
        for table in Base.metadata.sorted_tables:
            if "user_id" in table.c:
                assert not (
                    await session.execute(select(table).where(table.c.user_id == users[0].id))
                ).all()
        assert await session.get(User, users[0].id) is None
        assert await session.get(User, users[1].id) is not None


async def test_process_lock_exclusion_and_release_after_sigkill(database, configured, tmp_path):
    marker = tmp_path / "locked"
    script = """
import asyncio, sys
from pathlib import Path
from deadline_assistant.services.coordination import Coordination
async def main():
    coordination = Coordination(sys.argv[1])
    async with coordination.lock('process-test'):
        Path(sys.argv[2]).write_text('ready')
        await asyncio.Event().wait()
asyncio.run(main())
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        configured.database_url.get_secret_value(),
        str(marker),
        env=os.environ.copy(),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        async with asyncio.timeout(5):
            while not marker.exists():  # noqa: ASYNC110
                await asyncio.sleep(0.02)
        with pytest.raises(JobBusy):
            async with database.lock("process-test", wait=False):
                pytest.fail("Other process owns the lock")
        process.send_signal(signal.SIGKILL)
        await process.wait()
        async with database.lock("process-test", wait=False):
            pass
    finally:
        if process.returncode is None:
            process.kill()
        await process.communicate()


async def test_llm_concurrency_is_shared_between_independent_clients(database, configured):
    from test_llm import completion, mock_analyzer

    configured.llm_concurrency = 2
    active = 0
    peak = 0

    async def response(request):
        nonlocal active, peak
        active += 1
        peak = max(peak, active)
        await asyncio.sleep(0)
        active -= 1
        return completion('{"operations": []}')

    first = await mock_analyzer(configured, response)
    second = await mock_analyzer(configured, response)
    first.database = second.database = database
    try:
        async with asyncio.timeout(5):
            await asyncio.gather(
                *(client.analyze([], [], "Europe/Moscow") for client in [first, second] * 4)
            )
        assert peak == 2
    finally:
        await first.close()
        await second.close()
