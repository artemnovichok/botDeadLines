from unittest.mock import AsyncMock

import pytest
from sqlalchemy import func, select

from deadline_assistant.config import Settings
from deadline_assistant.db.models import AnalysisRun, Event, ProcessedMessage, SourceMessage, User
from deadline_assistant.db.session import Database
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
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.sync import SyncService


async def prepare(database: Database, user: User) -> object:
    async with database.transaction() as session:
        repo = TenantRepository(session, user.id)
        await repo.save_session("зашифровано")
        return await repo.track(-1001, "Учёба", {"kind": "chat", "id": 1})


async def test_sync_idempotent_and_no_ordinary_text_storage(
    database: Database, users: tuple[User, User], configured: Settings
) -> None:
    await prepare(database, users[0])
    messages = [
        MessageData(chat_id=-1001, message_id=i, text=f"Текст {i}", timestamp=utcnow())
        for i in (1, 2)
    ]
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = messages
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(
            operations=[
                CreateEvent(
                    event=EventData(title="Курсовая"),
                    evidence=Evidence(messages=[MessageRef(chat_id=-1001, message_id=1)]),
                )
            ]
        )
    )
    service = SyncService(database, reader, analyzer, configured)
    assert await service.sync_user(users[0].id) == 1
    assert await service.sync_user(users[0].id) == 0
    assert analyzer.analyze.await_count == 1
    async with database.transaction() as session:
        assert await session.scalar(select(func.count()).select_from(Event)) == 1
        assert await session.scalar(select(func.count()).select_from(SourceMessage)) == 1
        assert await session.scalar(select(func.count()).select_from(ProcessedMessage)) == 2
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 2
        assert await TenantRepository(session, users[1].id).active_events() == []


async def test_invalid_llm_reference_rolls_back_batch(
    database: Database, users: tuple[User, User], configured: Settings
) -> None:
    await prepare(database, users[0])
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=1, text="Сдать", timestamp=utcnow())
    ]
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(
            operations=[
                CreateEvent(
                    event=EventData(title="Валидное"),
                    evidence=Evidence(messages=[MessageRef(chat_id=-1001, message_id=1)]),
                ),
                CreateEvent(
                    event=EventData(title="Неверное"),
                    evidence=Evidence(messages=[MessageRef(chat_id=-1001, message_id=999)]),
                ),
            ]
        )
    )
    with pytest.raises(ValueError):
        await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    async with database.transaction() as session:
        for model in (Event, SourceMessage, ProcessedMessage):
            assert await session.scalar(select(func.count()).select_from(model)) == 0
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 0
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "failed"
        assert run.error == "AnalysisValidationError"


async def test_edit_creates_conflict_without_overwriting_event(
    database: Database, users: tuple[User, User], configured: Settings
) -> None:
    await prepare(database, users[0])
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Задание")
        )
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=1, text="Перенос", timestamp=utcnow())
    ]
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(
            operations=[
                CreateConflict(
                    event_id=event.id,
                    expected_version=event.version,
                    reason="Противоречие",
                    evidence=Evidence(messages=[MessageRef(chat_id=-1001, message_id=1)]),
                    variants=[
                        ConflictVariant(
                            event=EventData(title=title),
                            evidence=Evidence(messages=[MessageRef(chat_id=-1001, message_id=1)]),
                        )
                        for title in ("Задание", "Перенесённое задание")
                    ],
                )
            ]
        )
    )
    service = SyncService(database, reader, analyzer, configured)
    await service.sync_user(users[0].id)
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.event(event.id)).title == "Задание"
        assert len(await repo.conflicts()) == 1


async def test_sync_drains_backlog_in_committed_pages(
    database: Database, users: tuple[User, User], configured: Settings
) -> None:
    chat = await prepare(database, users[0])
    async with database.transaction() as session:
        stored = await TenantRepository(session, users[0].id).chat(chat.id)
        stored.last_processed_message_id = 1
        stored.initialization_status = "ready"
    configured.sync_batch_size = 2
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.side_effect = [
        [
            MessageData(chat_id=-1001, message_id=i, text="Обычный текст", timestamp=utcnow())
            for i in (2, 3)
        ],
        [MessageData(chat_id=-1001, message_id=4, text="Последнее", timestamp=utcnow())],
    ]
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(result=AnalysisResult())
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    assert reader.fetch.await_count == 2
    async with database.transaction() as session:
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 4
        assert await session.scalar(select(func.count()).select_from(ProcessedMessage)) == 3


async def test_first_analysis_batches_latest_history_and_persists_totals(
    database, users, configured
):
    await prepare(database, users[0])
    configured.sync_batch_size = 2
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=i, text="Доступная история", timestamp=utcnow())
        for i in range(101, 106)
    ]
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(result=AnalysisResult())
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    assert reader.fetch.await_count == 1
    assert [len(call.args[0]) for call in analyzer.analyze.await_args_list] == [2, 2, 1]
    async with database.transaction() as session:
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "succeeded"
        assert (run.chat_count, run.message_count, run.event_count) == (1, 5, 0)
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == chat.initial_upper_message_id == 105
        assert chat.initialization_status == "ready"


@pytest.mark.parametrize("disconnect", [False, True])
async def test_chat_or_account_disconnected_during_analysis_cannot_commit(
    database, users, configured, disconnect
):
    import asyncio

    from deadline_assistant.domain import IntegrationUnavailable
    from deadline_assistant.services.accounts import AccountService

    chat = await prepare(database, users[0])
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=1, text="Сдать", timestamp=utcnow())
    ]
    entered, finish = asyncio.Event(), asyncio.Event()

    async def analyze(*args):
        entered.set()
        await finish.wait()
        return AnalysisOutput(
            result=AnalysisResult(
                operations=[
                    CreateEvent(
                        event=EventData(title="Не сохранять после отключения"),
                        evidence=Evidence(messages=[MessageRef(chat_id=-1001, message_id=1)]),
                    ),
                ]
            )
        )

    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = analyze
    sync = SyncService(database, reader, analyzer, configured)
    task = asyncio.create_task(sync.sync_user(users[0].id))
    await entered.wait()
    accounts = AccountService(database, AsyncMock())
    if disconnect:
        await accounts.disconnect(users[0].id)
    else:
        await accounts.untrack(users[0].id, chat.id)
    finish.set()
    with pytest.raises(IntegrationUnavailable):
        await task
    async with database.transaction() as session:
        assert await TenantRepository(session, users[0].id).active_events() == []
        assert await session.scalar(select(func.count()).select_from(ProcessedMessage)) == 0
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "failed" and run.event_count == 0


async def test_partial_failure_keeps_committed_cursor_and_initial_upper_bound(
    database, users, configured
):
    await prepare(database, users[0])
    configured.sync_batch_size = 2
    messages = [
        MessageData(chat_id=-1001, message_id=i, text="История", timestamp=utcnow())
        for i in range(10, 15)
    ]
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = messages
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = [
        AnalysisOutput(result=AnalysisResult()),
        RuntimeError("upstream"),
    ]
    with pytest.raises(RuntimeError):
        await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    async with database.transaction() as session:
        chat = (await TenantRepository(session, users[0].id).chats())[0]
        assert chat.last_processed_message_id == 11
        assert chat.initial_upper_message_id == 14
        assert chat.initialization_status == "pending"
        run = await session.scalar(select(AnalysisRun))
        assert run.status == "failed" and run.message_count == 5


@pytest.mark.parametrize("include_recent", [False, True])
async def test_sparse_history_skips_old_messages_and_commits_checkpoint(
    database, users, configured, include_recent
):
    from datetime import timedelta

    await prepare(database, users[0])
    old = MessageData(
        chat_id=-1001,
        chat_title="Учёба",
        message_id=10,
        text="19 декабря КР № 2",
        timestamp=utcnow() - timedelta(days=365),
    )
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [old] + (
        [MessageData(chat_id=-1001, message_id=11, text="Учёба", timestamp=utcnow())]
        if include_recent
        else []
    )
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(result=AnalysisResult())
    service = SyncService(database, reader, analyzer, configured)
    await service.sync_user(users[0].id)
    await service.sync_user(users[0].id)
    assert analyzer.analyze.await_count == int(include_recent)
    if include_recent:
        assert [m.message_id for m in analyzer.analyze.await_args.args[0]] == [11]
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.chats())[0].last_processed_message_id == (11 if include_recent else 10)
        assert await repo.is_processed(old)
        assert await session.scalar(select(func.count()).select_from(SourceMessage)) == 0


async def test_sync_uses_saved_group_and_rejects_changed_profile(database, users, configured):
    from deadline_assistant.domain import StaleState
    from deadline_assistant.repositories.users import UserRepository

    await prepare(database, users[0])
    async with database.transaction() as session:
        await UserRepository(session).set_study_group(users[0].id, "бпи 267")
    reader = AsyncMock()
    reader.fetch_sources.return_value = {}
    reader.fetch.return_value = [
        MessageData(chat_id=-1001, message_id=1, text="Распределение КР", timestamp=utcnow())
    ]

    async def analyze(messages, events, timezone, context):
        assert context.study_group == "БПИ267"
        async with database.transaction() as session:
            await UserRepository(session).set_study_group(users[0].id, "БПИ268")
        return AnalysisOutput(result=AnalysisResult())

    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = analyze
    with pytest.raises(StaleState, match="Группа"):
        await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    async with database.transaction() as session:
        assert (await TenantRepository(session, users[0].id).chats())[
            0
        ].last_processed_message_id == 0
