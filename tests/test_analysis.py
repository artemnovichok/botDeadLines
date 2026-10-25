import json
from datetime import datetime
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from sqlalchemy import func, select

from deadline_assistant.db.models import SourceMessage, UserCorrection
from deadline_assistant.domain import (
    AddSource,
    AnalysisContext,
    AnalysisOutput,
    AnalysisResult,
    CancelEvent,
    CompleteEvent,
    ConflictVariant,
    CreateConflict,
    CreateEvent,
    EventData,
    Evidence,
    InputTooLarge,
    MessageData,
    MessageRef,
    StaleState,
    UpdateEvent,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository, event_data
from deadline_assistant.services.analysis import MemoryBuilder, OperationApplier, validate_result
from deadline_assistant.services.corrections import CorrectionService
from deadline_assistant.services.digest import build_digest
from deadline_assistant.services.sync import SyncService

CASES = json.loads((Path(__file__).parent / "fixtures/russian_cases.json").read_text())


def msg(i=1, text="7 ноября контрольная по алгебре", **changes):
    return MessageData.model_validate(
        {
            "chat_id": -1001,
            "chat_title": "Учебная группа",
            "message_id": i,
            "text": text,
            "timestamp": "2026-10-12T10:00:00+03:00",
            **changes,
        }
    )


def evidence(i=1, **changes):
    return Evidence(messages=[MessageRef(chat_id=-1001, message_id=i)], **changes)


async def context_for(database, user, messages, **kwargs):
    async with database.transaction() as session:
        return await MemoryBuilder().build(
            TenantRepository(session, user.id), messages, user.timezone, **kwargs
        )


async def apply(database, user, operations, messages, context):
    async with database.transaction() as session:
        return await OperationApplier().apply(
            TenantRepository(session, user.id),
            AnalysisResult(operations=operations),
            messages,
            context,
        )


@pytest.mark.parametrize("case", CASES, ids=[c["name"] for c in CASES])
async def test_russian_fixture_output_validation_and_persistence(database, users, case):
    message = msg(text=case["text"], timestamp=case["timestamp"])
    operations = (
        []
        if case["event"] is None
        else [CreateEvent(event=EventData(**case["event"], confidence=0.9), evidence=evidence())]
    )
    context = await context_for(database, users[0], [message], now=message.timestamp)
    assert await apply(database, users[0], operations, [message], context) == len(operations)
    async with database.transaction() as session:
        events = await TenantRepository(session, users[0].id).active_events()
        assert len(events) == len(operations)
        if events:
            assert event_data(events[0]) == operations[0].event
            if events[0].resubmission_end:
                assert events[0].retain_until > events[0].resubmission_end


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "create_event", "event": {"title": "Задание"}, "evidence": {}},
        {
            "op": "cancel_event",
            "event_id": "made-up",
            "expected_version": 1,
            "reason": "Отмена",
            "evidence": {"messages": [{"chat_id": -1, "message_id": 1}]},
        },
        {"op": "execute_sql", "sql": "DELETE FROM events"},
        {
            "op": "create_event",
            "event": {"title": "Ложный ID"},
            "evidence": {"messages": [{"chat_id": -1001, "message_id": True}]},
        },
        {
            "op": "create_event",
            "event": {"title": "Неверная обязательность", "is_optional": "false"},
            "evidence": {"messages": [{"chat_id": -1001, "message_id": 1}]},
        },
        {"op": "no_action", "reason": "нет", "user_id": "другой"},
    ],
)
def test_operation_schema_rejects_untrusted_commands(operation):
    with pytest.raises(ValidationError):
        AnalysisResult.model_validate({"operations": [operation]})


async def test_deterministic_duplicate_creation_merges_sources(database, users):
    data = EventData(title="КР по алгебре", confidence=0.9)
    for i in (1, 2):
        context = await context_for(database, users[0], [msg(i)])
        count = await apply(
            database, users[0], [CreateEvent(event=data, evidence=evidence(i))], [msg(i)], context
        )
        assert count == int(i == 1)
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        events = await repo.active_events()
        assert len(events) == 1 and len(await repo.sources(events[0].id)) == 2


@pytest.mark.parametrize("kind", ["update", "cancel", "complete", "source"])
async def test_all_target_operations_and_foreign_ownership(database, users, kind):
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Задание")
        )
    context = await context_for(database, users[0], [msg()])
    fields = dict(
        event_id=event.id,
        expected_version=event.version,
        evidence=evidence(),
        reason="Явное уточнение",
    )
    classes = {
        "update": UpdateEvent,
        "cancel": CancelEvent,
        "complete": CompleteEvent,
        "source": AddSource,
    }
    operation = classes[kind](
        **fields,
        **({"event": EventData(title="Новое", confidence=0.95)} if kind == "update" else {}),
    )
    with pytest.raises(StaleState):
        await apply(database, users[1], [operation], [msg()], context)
    await apply(database, users[0], [operation], [msg()], context)
    async with database.transaction() as session:
        saved = await TenantRepository(session, users[0].id).event(event.id)
        assert saved.version > event.version
        assert saved.status == {"cancel": "cancelled", "complete": "completed"}.get(kind, "active")
        if kind == "update":
            assert saved.title == "Новое"


async def test_three_variants_dedup_stale_owner_and_fast_resolution(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Алгебра", confidence=0.9))
        sources = [await repo.source(msg(i, text=f"КР {i} ноября")) for i in (1, 2, 3)]
        await repo.attach_source(event.id, sources[0].id)
        alternatives = [
            EventData(
                title="Алгебра",
                due_at=f"2026-11-0{i}T00:00:00+03:00",
                date_precision="day",
                confidence=0.9,
            )
            for i in (2, 3)
        ]
        conflict = await repo.create_conflict(
            event.id,
            variants=[
                (data, [source.id], [], "")
                for data, source in zip(alternatives, sources[1:], strict=True)
            ],
        )
        version = conflict.event_version
        again = await repo.create_conflict(event.id, alternatives[0], [sources[1].id])
        assert conflict.id == again.id and again.event_version == version
        assert len(await repo.conflict_options(conflict.id)) == 3
    from deadline_assistant.domain import EntityNotFound

    with pytest.raises(EntityNotFound):
        async with database.transaction() as session:
            await TenantRepository(session, users[1].id).resolve_conflict(conflict.id, 3, version)
    async with database.transaction() as session:
        cards, summary = await build_digest(
            TenantRepository(session, users[0].id), users[0].timezone
        )
        assert "Дата требует подтверждения" in summary and "3 ноября 2026" in summary
        assert len([b for b in cards[0].buttons if b[1].startswith("cf:")]) == 4
        assert "КР 3 ноября" not in cards[0].text
        saved_sources = await TenantRepository(session, users[0].id).sources(
            event.id, include_conflicts=True
        )
        assert any(s.original_text == "КР 3 ноября" for s in saved_sources)
        await TenantRepository(session, users[0].id).resolve_conflict(conflict.id, 3, version)
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.event(event.id)).due_at == alternatives[1].due_at
        assert not await repo.conflicts()


async def test_stale_conflict_cannot_overwrite_manual_change(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Алгебра"))
        source = await repo.source(msg())
        conflict = await repo.create_conflict(event.id, EventData(title="Перенос"), [source.id])
        prepared_version = conflict.event_version
        await repo.write_event(event, EventData(title="Свежая ручная правка"))
    with pytest.raises(StaleState):
        async with database.transaction() as session:
            await TenantRepository(session, users[0].id).resolve_conflict(
                conflict.id, 2, prepared_version
            )
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        cards, _ = await build_digest(repo, users[0].timezone)
        assert cards[0].buttons[0][0] == "Проверить снова"
        assert (await repo.event(event.id)).title == "Свежая ручная правка"


async def test_immediate_free_text_correction_replay_and_sql_memory(database, users, configured):
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Алгебра")
        )
    analyzer = AsyncMock(model="mock")

    async def reply(messages, events, timezone, context):
        assert not messages
        correction = next(c for c in context.corrections if c.id == context.focus_correction_id)
        assert correction.text == "Контрольная будет 12 ноября, а не 7-го"
        memory = next(e for e in context.events if e.id == event.id)
        return AnalysisOutput(
            result=AnalysisResult(
                operations=[
                    UpdateEvent(
                        event_id=event.id,
                        expected_version=memory.version,
                        reason="Пользователь уточнил",
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

    analyzer.analyze.side_effect = reply
    service = CorrectionService(database, analyzer, configured)
    text = "Контрольная будет 12 ноября, а не 7-го"
    assert await service.submit(users[0].id, event.id, text, 101, 12) == "applied"
    assert await service.submit(users[0].id, event.id, text, 101, 12) == "applied"
    assert analyzer.analyze.await_count == 1
    memory = await context_for(database, users[0], [msg(2, "Преподаватель: 14 ноября")])
    assert memory.corrections[0].status == "applied"
    assert memory.corrections[0].text == text


async def test_correction_failure_remains_pending(database, users, configured):
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Алгебра")
        )
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await CorrectionService(database, analyzer, configured).submit(
            users[0].id, event.id, "12 ноября"
        )
    async with database.transaction() as session:
        correction = await session.scalar(select(UserCorrection))
        assert correction.status == "pending"
        assert (await TenantRepository(session, users[0].id).event(event.id)).due_at is None


async def test_context_excludes_expired_includes_grace_and_reply(database, users):
    now = datetime.fromisoformat("2026-10-20T09:00:00+03:00")
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        old = await repo.create_event(
            EventData(
                title="Старое задание",
                subject="БЖД",
                due_at="2026-10-15T00:00:00+03:00",
                date_precision="day",
            )
        )
        grace = await repo.create_event(
            EventData(
                title="Лабораторная",
                due_at="2026-10-15T00:00:00+03:00",
                date_precision="day",
                resubmission_end="2026-10-22T00:00:00+03:00",
            )
        )
        source = await repo.source(msg())
        await repo.attach_source(old.id, source.id)
        await repo.complete(grace.id)
        context = await MemoryBuilder().build(repo, [], "Europe/Moscow", now=now)
        assert not context.events
        grace.status = "active"
        context = await MemoryBuilder().build(repo, [], "Europe/Moscow", now=now)
        assert [e.id for e in context.events] == [grace.id]
        context = await MemoryBuilder().build(
            repo, [msg(3, reply_to_message_id=1)], "Europe/Moscow", now=now
        )
        assert {e.id for e in context.events} == {old.id, grace.id}


async def test_unknown_cross_chat_evidence_and_corrections_rejected(database, users):
    context = AnalysisContext()
    operation = CreateEvent(
        event=EventData(title="Взлом"),
        evidence=Evidence(messages=[MessageRef(chat_id=-999, message_id=1)]),
    )
    with pytest.raises(ValueError):
        validate_result(AnalysisResult(operations=[operation]), [msg()], context)


async def test_llm_result_cannot_commit_over_newer_event_version(database, users):
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Алгебра")
        )
    context = await context_for(database, users[0], [msg()])
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.write_event(await repo.event(event.id), EventData(title="Новое уточнение"))
    operation = CancelEvent(
        event_id=event.id,
        expected_version=event.version,
        evidence=evidence(),
        reason="Старая отмена",
    )
    with pytest.raises(StaleState):
        await apply(database, users[0], [operation], [msg()], context)


async def test_long_noisy_chat_chunk_retry_checkpoint_and_deleted_cursor(
    database, users, configured
):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.save_session("encrypted")
        chat = await repo.track(-1001, "Учёба", {"kind": "chat", "id": 1})
    messages = [msg(i, "С днём рождения! 😂 Ну и фигня эта пара...") for i in range(1, 1001)]
    messages[699] = msg(700)
    reader = AsyncMock()
    reader.fetch.return_value = messages
    reader.fetch_sources.return_value = {}
    analyzer = AsyncMock(model="mock")

    async def analyze(batch, events, timezone, context):
        if len(batch) > 20:
            raise InputTooLarge()
        operations = (
            [
                CreateEvent(
                    event=EventData(title="КР по алгебре", confidence=0.9), evidence=evidence(700)
                )
            ]
            if any(m.message_id == 700 for m in batch)
            else []
        )
        return AnalysisOutput(result=AnalysisResult(operations=operations))

    analyzer.analyze.side_effect = analyze
    sync = SyncService(database, reader, analyzer, configured)
    assert await sync.sync_user(users[0].id) == 1
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.chat(chat.id)).last_processed_message_id == 1000
        assert await session.scalar(select(func.count()).select_from(SourceMessage)) == 1
    # ID 1000 no longer exists; requesting >1000 must still process 1001.
    reader.fetch.return_value = [msg(1001, "Просто новое сообщение")]
    assert await sync.sync_user(users[0].id) == 0
    assert reader.fetch.call_args.args[1].last_processed_message_id == 1000
    async with database.transaction() as session:
        assert (
            await TenantRepository(session, users[0].id).chat(chat.id)
        ).last_processed_message_id == 1001


async def test_saved_source_edited_then_deleted_outside_lookback(database, users, configured):
    original = msg(1)
    edited = msg(
        1, text="КР по алгебре перенесли на 12 ноября", edit_timestamp="2026-10-14T10:00:00+03:00"
    )
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.save_session("encrypted")
        chat = await repo.track(-1001, "Учёба", {"kind": "chat", "id": 1})
        chat.last_processed_message_id = 500
        chat.initialization_status = "ready"
        event = await repo.create_event(
            EventData(
                title="КР по алгебре",
                due_at="2026-11-07T00:00:00+03:00",
                date_precision="day",
                confidence=0.9,
            )
        )
        source = await repo.source(original)
        await repo.attach_source(event.id, source.id)
        await repo.mark_processed(original)
    reader = AsyncMock()
    reader.fetch.return_value = []
    reader.fetch_sources.return_value = {1: edited}
    analyzer = AsyncMock(model="mock")

    async def analyze(batch, events, timezone, context):
        assert [m.message_id for m in batch] == [1]
        assert context.sources[0].message.text == original.text
        memory = next(e for e in context.events if e.id == event.id)
        return AnalysisOutput(
            result=AnalysisResult(
                operations=[
                    UpdateEvent(
                        event_id=event.id,
                        expected_version=memory.version,
                        reason="Сообщение отредактировано",
                        event=EventData(
                            title="КР по алгебре",
                            due_at="2026-11-12T00:00:00+03:00",
                            date_precision="day",
                            confidence=0.95,
                        ),
                        evidence=evidence(),
                    )
                ]
            )
        )

    analyzer.analyze.side_effect = analyze
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    reader.fetch_sources.assert_awaited_once()
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.chat(chat.id)).last_processed_message_id == 500
        assert len(await repo.sources(event.id)) == 2
    # Deletion preserves both historical versions and the event; no LLM call.
    reader.fetch_sources.return_value = {1: None}
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    assert analyzer.analyze.await_count == 1
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        saved = await repo.event(event.id)
        assert saved.status == "active" and saved.due_at.day == 11  # UTC: previous evening
        sources = await repo.sources(event.id)
        assert all(not s.available and s.original_text for s in sources)


async def test_failed_source_edit_analysis_preserves_checkpoint_and_text(
    database, users, configured
):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.save_session("encrypted")
        chat = await repo.track(-1001, "Учёба", {"kind": "chat", "id": 1})
        chat.last_processed_message_id = 500
        chat.initialization_status = "ready"
        event = await repo.create_event(EventData(title="Работа"))
        source = await repo.source(msg())
        await repo.attach_source(event.id, source.id)
    reader = AsyncMock()
    reader.fetch_sources.return_value = {1: msg(1, text="Срок изменён", edit_timestamp=utcnow())}
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.side_effect = TimeoutError()
    with pytest.raises(TimeoutError):
        await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.chat(chat.id)).last_processed_message_id == 500
        assert (await repo.sources(event.id))[0].original_text == msg().text


async def test_teacher_confirmation_then_student_rumour_keeps_both_sources(database, users):
    teacher = msg(1, "7 ноября контрольная", author_role="teacher", author_name="Ирина Петровна")
    rumour = msg(
        2, "Мне кажется, перенесли на 12 ноября", author_role="student", author_name="Саша"
    )
    first = EventData(
        title="КР по алгебре",
        due_at="2026-11-07T00:00:00+03:00",
        date_precision="day",
        confidence=0.95,
    )
    second = first.model_copy(
        update={"due_at": datetime.fromisoformat("2026-11-12T00:00:00+03:00")}
    )
    context = await context_for(database, users[0], [teacher])
    await apply(
        database, users[0], [CreateEvent(event=first, evidence=evidence())], [teacher], context
    )
    context = await context_for(database, users[0], [rumour])
    memory = context.events[0]
    operation = CreateConflict(
        event_id=memory.id,
        expected_version=memory.version,
        evidence=evidence(2),
        reason="Слух противоречит сообщению преподавателя",
        variants=[
            ConflictVariant(event=first, evidence=Evidence(source_ids=memory.source_ids)),
            ConflictVariant(event=second, evidence=evidence(2)),
        ],
    )
    await apply(database, users[0], [operation], [rumour], context)
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.event(memory.id)).due_at == first.due_at
        cards, summary = await build_digest(repo, users[0].timezone)
        assert "7 ноября 2026" in summary and "12 ноября 2026" in summary
        assert "Мне кажется" not in cards[0].text
        saved_sources = await repo.sources(memory.id, include_conflicts=True)
        assert any("Мне кажется" in (s.original_text or "") for s in saved_sources)


async def test_unconfirmed_conflict_does_not_block_other_events(database, users, settings):
    from deadline_assistant.services.retention import RetentionService
    from deadline_assistant.services.scheduler import SchedulerService

    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.save_session("encrypted")
        disputed = await repo.create_event(EventData(title="Спорная КР"))
        await repo.create_event(EventData(title="Подтверждённая лабораторная"))
        source = await repo.source(msg())
        conflict = await repo.create_conflict(disputed.id, EventData(title="Перенос"), [source.id])
        await repo.resolve_conflict(conflict.id, 0, conflict.event_version)
    notifier = AsyncMock()
    scheduler = SchedulerService(
        database, settings, AsyncMock(), notifier, RetentionService(database, settings)
    )
    await scheduler.tick(datetime.fromisoformat("2026-10-20T09:00:00+03:00"))
    notifier.send_conflict.assert_awaited_once()
    summary = next(c.args[1] for c in notifier.send.await_args_list if c.args[0] == 101)
    assert "Подтверждённая лабораторная" in summary and "карточку конфликта" in summary
    deliveries = [c[0] for c in notifier.mock_calls if c[0] in {"send", "send_conflict"}]
    assert deliveries[0] == "send_conflict"


async def test_pending_correction_is_retried_even_without_new_chat_messages(
    database, users, configured
):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.save_session("encrypted")
        await repo.track(-1001, "Учёба", {"kind": "chat", "id": 1})
        event = await repo.create_event(EventData(title="Алгебра"))
        correction = await repo.correction(event.id, "Дедлайн 12 ноября")
    reader, analyzer = AsyncMock(model="mock"), AsyncMock(model="mock")
    reader.fetch.return_value = []

    async def analyze(messages, events, timezone, context):
        assert messages == [] and context.focus_correction_id == correction.id
        memory = next(e for e in context.events if e.id == event.id)
        return AnalysisOutput(
            result=AnalysisResult(
                operations=[
                    UpdateEvent(
                        event_id=event.id,
                        expected_version=memory.version,
                        reason="Повтор сохранённой правки",
                        event=EventData(
                            title="Алгебра",
                            due_at="2026-11-12T00:00:00+03:00",
                            date_precision="day",
                            confidence=0.9,
                        ),
                        evidence=Evidence(correction_ids=[correction.id]),
                    )
                ]
            )
        )

    analyzer.analyze.side_effect = analyze
    await SyncService(database, reader, analyzer, configured).sync_user(users[0].id)
    analyzer.analyze.assert_awaited_once()
    async with database.transaction() as session:
        assert (
            await TenantRepository(session, users[0].id).correction_by_id(correction.id)
        ).status == "applied"


async def test_low_confidence_update_creates_conflict_and_resolves_correction(
    database, users, configured
):
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Алгебра")
        )
    analyzer = AsyncMock(model="mock")

    async def analyze(messages, events, timezone, context):
        memory = context.events[0]
        return AnalysisOutput(
            result=AnalysisResult(
                operations=[
                    UpdateEvent(
                        event_id=event.id,
                        expected_version=memory.version,
                        reason="Возможно, другой срок",
                        event=EventData(
                            title="Алгебра",
                            due_at="2026-11-12T00:00:00+03:00",
                            date_precision="day",
                            confidence=0.4,
                        ),
                        evidence=Evidence(correction_ids=[context.focus_correction_id]),
                    )
                ]
            )
        )

    analyzer.analyze.side_effect = analyze
    service = CorrectionService(database, analyzer, configured)
    assert await service.submit(users[0].id, event.id, "Наверное 12 ноября") == "conflict"
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.event(event.id)).due_at is None
        conflict = (await repo.conflicts())[0]
        await repo.resolve_conflict(conflict.id, 2, conflict.event_version)
        correction = await session.scalar(select(UserCorrection))
        assert correction.status == "applied"
    analyzer.analyze.assert_awaited_once()


async def test_conflict_choices_expose_conditions_place_and_grace(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="ДЗ БЖД"))
        source = await repo.source(msg())
        await repo.create_conflict(
            event.id,
            EventData(
                title="ДЗ БЖД",
                is_optional=True,
                location="Аудитория 301",
                submission_conditions="Для оценки 10",
                penalties="Минус балл",
                resubmission_end="2026-11-18T00:00:00+03:00",
            ),
            [source.id],
        )
        cards, _ = await build_digest(repo, "Europe/Moscow")
        for text in ("Необязательно", "Аудитория 301", "Для оценки 10", "Минус балл", "18.11.2026"):
            assert text in cards[0].text


async def test_recheck_is_scoped_to_the_selected_conflict(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        first = await repo.create_event(EventData(title="Алгебра"))
        other = await repo.create_event(EventData(title="БЖД"))
        source = await repo.source(msg())
        conflict = await repo.create_conflict(first.id, EventData(title="КР"), [source.id])
        context = await MemoryBuilder().build(
            repo, [], "Europe/Moscow", recheck_conflict_id=conflict.id
        )
    operation = CancelEvent(
        event_id=other.id,
        expected_version=other.version,
        evidence=Evidence(source_ids=[source.id]),
        reason="Чужое событие в контексте",
    )
    with pytest.raises(ValueError, match="выбранному конфликту"):
        validate_result(AnalysisResult(operations=[operation]), [], context)


async def test_uncertain_new_event_is_marked_for_clarification(database, users):
    context = await context_for(database, users[0], [msg()])
    operation = CreateEvent(
        event=EventData(title="Возможная КР", confidence=0.4), evidence=evidence()
    )
    await apply(database, users[0], [operation], [msg()], context)
    async with database.transaction() as session:
        _, summary = await build_digest(TenantRepository(session, users[0].id), "Europe/Moscow")
    assert "Дата требует подтверждения" in summary


@pytest.mark.parametrize("grace", [None, "2026-11-01T00:00:00+03:00"])
async def test_past_creation_is_ignored_even_if_uncertain(database, users, grace):
    context = await context_for(
        database, users[0], [msg()], now=datetime.fromisoformat("2026-10-20T12:00:00+03:00")
    )
    operation = CreateEvent(
        event=EventData(
            title="КР № 2",
            due_at="2025-12-19T00:00:00+03:00",
            date_precision="day",
            needs_clarification=True,
            confidence=0.4,
            resubmission_end=grace,
        ),
        evidence=evidence(),
    )
    assert await apply(database, users[0], [operation], [msg()], context) == int(grace is not None)
    async with database.transaction() as session:
        assert len(await TenantRepository(session, users[0].id).active_events()) == int(
            grace is not None
        )
        assert await session.scalar(select(func.count()).select_from(SourceMessage)) == int(
            grace is not None
        )


async def test_expired_uncertain_event_is_not_in_unrelated_memory(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        await repo.create_event(
            EventData(
                title="КР № 2",
                due_at="2025-12-19T00:00:00+03:00",
                date_precision="day",
                needs_clarification=True,
            )
        )
        context = await MemoryBuilder().build(repo, [msg()], "Europe/Moscow")
        assert context.events == []
