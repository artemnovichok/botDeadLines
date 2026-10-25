from datetime import timedelta

import pytest
from sqlalchemy import select

from deadline_assistant.db.models import Event
from deadline_assistant.domain import (
    AnalysisContext,
    AnalysisResult,
    CreateEvent,
    EntityNotFound,
    EventData,
    Evidence,
    MessageData,
    MessageRef,
    StaleState,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository, event_data
from deadline_assistant.services.analysis import OperationApplier
from deadline_assistant.services.retention import RetentionService


async def test_teacher_update_and_conflict_cannot_reset_user_completion(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="КР"))
        await repo.complete(event.id)
        completed_at = event.completed_at
        await repo.write_event(event, EventData(title="КР перенесена"))
        assert event.status == "completed" and event.completed_at == completed_at
        conflict = await repo.create_conflict(event.id, EventData(title="Другой срок"))
        await repo.resolve_conflict(conflict.id, 2)
        assert event.status == "completed" and event.completed_at == completed_at
        assert await repo.active_events() == []


async def test_duplicate_teacher_message_does_not_recreate_completed_task(database, users):
    message = MessageData(chat_id=-1, message_id=1, text="КР", timestamp=utcnow())
    data = EventData(title="КР")
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(data)
        await repo.complete(event.id)
        operation = CreateEvent(
            event=data, evidence=Evidence(messages=[MessageRef(chat_id=-1, message_id=1)])
        )
        await OperationApplier().apply(
            repo, AnalysisResult(operations=[operation]), [message], AnalysisContext()
        )
        assert event.status == "completed" and await repo.conflicts() == []
        assert len(list(await session.scalars(select(Event)))) == 1


async def test_purged_completed_task_leaves_small_identity_marker(database, users, settings):
    data = EventData(title="Законченная работа")
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(data)
        await repo.complete(event.id)
        event.completed_at = utcnow() - timedelta(days=40)
    await RetentionService(database, settings).cleanup()
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert await session.get(Event, event.id) is None
        message = MessageData(
            chat_id=-1, message_id=2, text="Законченная работа", timestamp=utcnow()
        )
        operation = CreateEvent(
            event=data, evidence=Evidence(messages=[MessageRef(chat_id=-1, message_id=2)])
        )
        count = await OperationApplier().apply(
            repo, AnalysisResult(operations=[operation]), [message], AnalysisContext()
        )
        assert count == 0 and await repo.active_events() == []


async def test_undo_preserves_completion_and_can_unwind_multiple_corrections(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Изначально"))
        first = await repo.correction(event.id, "Первая правка")
        await repo.apply_correction(first.id, EventData(title="Первое изменение"))
        second = await repo.correction(event.id, "Вторая правка")
        await repo.apply_correction(second.id, EventData(title="Второе изменение"))
        await repo.complete(event.id)
        await repo.undo_correction(event.id)
        assert event.title == "Первое изменение" and event.status == "completed"
        await repo.undo_correction(event.id)
        assert event.title == "Изначально" and event.status == "completed"
        assert first.status == second.status == "reverted"


async def test_undo_rejects_new_information_instead_of_overwriting(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Старое"))
        correction = await repo.correction(event.id, "Правка")
        await repo.apply_correction(correction.id, EventData(title="Исправлено"))
        await repo.write_event(event, EventData(title="Новый ответ преподавателя"))
        with pytest.raises(StaleState):
            await repo.undo_correction(event.id)
        assert event.title == "Новый ответ преподавателя"


async def test_pending_correction_withdrawal_invalidates_inflight_version(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Работа"))
        correction = await repo.correction(event.id, "Перенос")
        version = event.version
        await repo.undo_correction(event.id)
        assert correction.status == "reverted" and event.version > version
        assert event_data(event).title == "Работа"


async def test_list_versions_expiry_and_owner_checks(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Работа"))
        listing = await repo.save_event_list([event])
        assert (await repo.listed_event(listing.id, event.id)).id == event.id
        with pytest.raises(StaleState):
            await TenantRepository(session, users[1].id).listed_event(listing.id, event.id)
        with pytest.raises(EntityNotFound):
            await TenantRepository(session, users[1].id).undo_correction(event.id)
        await repo.write_event(event, EventData(title="Изменено"))
        with pytest.raises(StaleState):
            await repo.listed_event(listing.id, event.id)
        listing.expires_at = utcnow() - timedelta(seconds=1)
        await session.flush()
        with pytest.raises(StaleState):
            await repo.event_list(listing.id)


async def test_withdrawing_correction_keeps_teacher_conflict_for_recheck(database, users):
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        event = await repo.create_event(EventData(title="Исходное"))
        correction = await repo.correction(event.id, "Моя версия")
        correction.status = "conflict"
        conflict = await repo.create_conflict(
            event.id,
            variants=[
                (EventData(title="Версия преподавателя"), [], [], "Преподаватель"),
                (EventData(title="Моя версия"), [], [correction.id], "Пользователь"),
            ],
        )
        version = conflict.event_version
        await repo.undo_correction(event.id)
        assert (await repo.conflict(conflict.id)).status == "open"
        assert conflict.event_version == version < event.version
        with pytest.raises(StaleState):
            await repo.resolve_conflict(conflict.id, 3, version)
