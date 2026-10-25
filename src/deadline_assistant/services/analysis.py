"""SQL-backed memory and the only interpreter of untrusted LLM operations."""

from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from deadline_assistant.db.models import (
    ConflictOption,
    ConflictOptionSource,
    Event,
    EventSource,
    EventTombstone,
    SourceMessage,
    User,
    UserCorrection,
)
from deadline_assistant.domain import (
    AddSource,
    AnalysisContext,
    AnalysisResult,
    AnalysisValidationError,
    CancelEvent,
    CompleteEvent,
    CorrectionMemory,
    CreateConflict,
    CreateEvent,
    EventMemory,
    EventStatus,
    Evidence,
    InputTooLarge,
    MessageData,
    NoAction,
    SourceMemory,
    StaleState,
    TargetOperation,
    UpdateEvent,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository, event_data, source_message
from deadline_assistant.services.lifecycle import expired


class MemoryBuilder:
    def __init__(self, event_limit: int = 200) -> None:
        self.event_limit = event_limit

    async def build(
        self,
        repo: TenantRepository,
        messages: list[MessageData],
        timezone: str,
        *,
        focus_correction_id: UUID | None = None,
        recheck_conflict_id: UUID | None = None,
        now: datetime | None = None,
    ) -> AnalysisContext:
        now = now or utcnow()
        events = list(
            await repo.session.scalars(select(Event).where(Event.user_id == repo.user_id))
        )
        corrections = list(
            await repo.session.scalars(
                select(UserCorrection)
                .where(UserCorrection.user_id == repo.user_id)
                .order_by(UserCorrection.created_at)
            )
        )
        forced = {c.event_id for c in corrections if c.status in {"pending", "conflict"}}
        conflicts = await repo.conflicts()
        forced.update(c.event_id for c in conflicts)
        if focus_correction_id:
            forced.add((await repo.correction_by_id(focus_correction_id)).event_id)
        recheck_event_id = None
        if recheck_conflict_id:
            recheck_event_id = (await repo.conflict(recheck_conflict_id)).event_id
            forced.add(recheck_event_id)
        all_sources = list(
            await repo.session.scalars(
                select(SourceMessage)
                .join(EventSource, EventSource.source_id == SourceMessage.id)
                .where(SourceMessage.user_id == repo.user_id, EventSource.user_id == repo.user_id)
            )
        )
        reply_refs = {(m.chat_id, m.reply_to_message_id) for m in messages}
        reply_refs.update((m.chat_id, m.message_id) for m in messages)
        related_source_ids = {
            s.id for s in all_sources if (s.source_chat_id, s.source_message_id) in reply_refs
        }
        if related_source_ids:
            forced.update(
                await repo.session.scalars(
                    select(EventSource.event_id).where(
                        EventSource.user_id == repo.user_id,
                        EventSource.source_id.in_(related_source_ids),
                    )
                )
            )
        text = "\n".join((m.text + "\n" + m.chat_title).casefold() for m in messages)
        message_chats = {m.chat_id for m in messages}
        chat_source_ids = {s.id for s in all_sources if s.source_chat_id in message_chats}
        detailed = set(forced)
        if chat_source_ids:
            detailed.update(
                await repo.session.scalars(
                    select(EventSource.event_id).where(
                        EventSource.user_id == repo.user_id,
                        EventSource.source_id.in_(chat_source_ids),
                    )
                )
            )
        selected = []
        for event in events:
            # Retrieval of older entities is not a message classifier.
            related = bool(event.subject and event.subject.casefold() in text) or (
                len(event.title) >= 8 and event.title.casefold() in text
            )
            if related:
                detailed.add(event.id)
            current = event.status == "active" and (
                not expired(event_data(event), timezone, now)
                or (
                    event.due_at is not None
                    and event.due_at.astimezone(ZoneInfo(timezone)).date()
                    == now.astimezone(ZoneInfo(timezone)).date()
                )
            )
            if current or related or event.id in forced:
                selected.append(event)
        if len(selected) > self.event_limit:
            raise InputTooLarge("Слишком много актуальных событий для одного контекста")
        selected_ids = {e.id for e in selected}
        sources_by_id: dict[UUID, SourceMessage] = {}
        event_memories = []
        for event in selected:
            # Keep the active event catalog for cross-chat matching, but retrieve full
            # evidence only for this chat, related events, corrections and conflicts.
            sources = await repo.current_sources(event.id) if event.id in detailed else []
            sources_by_id.update((s.id, s) for s in sources)
            event_memories.append(
                EventMemory(
                    id=event.id,
                    version=event.version,
                    data=event_data(event),
                    source_ids=[s.id for s in sources],
                )
            )
        # Conflicting historical versions must remain independently inspectable.
        conflict_ids = [c.id for c in conflicts if c.event_id in selected_ids]
        if conflict_ids:
            option_sources = list(
                await repo.session.scalars(
                    select(SourceMessage)
                    .join(ConflictOptionSource, ConflictOptionSource.source_id == SourceMessage.id)
                    .join(ConflictOption, ConflictOption.id == ConflictOptionSource.option_id)
                    .where(
                        SourceMessage.user_id == repo.user_id,
                        ConflictOption.user_id == repo.user_id,
                        ConflictOption.conflict_id.in_(conflict_ids),
                    )
                )
            )
            sources_by_id.update((s.id, s) for s in option_sources)
        recent_applied: dict[UUID, UserCorrection] = {}
        unresolved = []
        for correction in corrections:
            if correction.event_id not in selected_ids:
                continue
            if correction.status in {"pending", "conflict"}:
                unresolved.append(correction)
            elif correction.status == "applied":
                recent_applied[correction.event_id] = correction
        return AnalysisContext(
            current_time=now,
            timezone=timezone,
            study_group=await repo.session.scalar(
                select(User.study_group).where(User.id == repo.user_id)
            ),
            events=event_memories,
            sources=[
                SourceMemory(id=s.id, message=source_message(s), available=s.available)
                for s in sources_by_id.values()
            ],
            corrections=[
                CorrectionMemory(
                    id=c.id,
                    event_id=c.event_id,
                    text=c.original_text,
                    created_at=c.created_at,
                    status=c.status,
                )
                for c in [*recent_applied.values(), *unresolved]
            ],
            focus_correction_id=focus_correction_id,
            recheck_conflict_id=recheck_conflict_id,
            recheck_event_id=recheck_event_id,
        )


def operation_evidence(result: AnalysisResult) -> list[Evidence]:
    items = []
    for operation in result.operations:
        if isinstance(operation, NoAction):
            continue
        items.append(operation.evidence)
        if isinstance(operation, CreateConflict):
            items.extend(v.evidence for v in operation.variants)
    return items


def validate_result(
    result: AnalysisResult, messages: list[MessageData], context: AnalysisContext
) -> None:
    refs = {(m.chat_id, m.message_id) for m in messages}
    source_ids = {s.id for s in context.sources}
    corrections = {c.id: c for c in context.corrections}
    events = {e.id: e for e in context.events}
    for evidence in operation_evidence(result):
        if any((r.chat_id, r.message_id) not in refs for r in evidence.messages):
            raise AnalysisValidationError("ИИ сослался на неизвестное сообщение")
        if not set(evidence.source_ids) <= source_ids:
            raise AnalysisValidationError("ИИ сослался на неизвестный сохранённый источник")
        if not set(evidence.correction_ids) <= corrections.keys():
            raise AnalysisValidationError("ИИ сослался на неизвестное исправление")
    mutated: set[UUID] = set()
    for operation in result.operations:
        if isinstance(operation, NoAction):
            continue
        proposed_data = (
            [v.event for v in operation.variants]
            if isinstance(operation, CreateConflict)
            else [operation.event]
            if isinstance(operation, (CreateEvent, UpdateEvent))
            else []
        )
        if any(d.timezone != context.timezone for d in proposed_data):
            raise AnalysisValidationError(
                "Часовой пояс результата отличается от настроек пользователя"
            )
        if isinstance(operation, CreateEvent):
            if context.focus_correction_id is not None or context.recheck_conflict_id is not None:
                raise AnalysisValidationError(
                    "Исправление и повторная проверка не создают новые события"
                )
            if operation.event.status != "active" or not operation.evidence.messages:
                raise AnalysisValidationError(
                    "Новое событие требует новое сообщение и активный статус"
                )
            if operation.evidence.correction_ids:
                raise AnalysisValidationError("Исправление не создаёт независимое событие")
            continue
        if context.focus_correction_id is not None:
            focus = corrections.get(context.focus_correction_id)
            if (
                focus is None
                or operation.event_id != focus.event_id
                or context.focus_correction_id not in operation.evidence.correction_ids
            ):
                raise AnalysisValidationError(
                    "Правка должна относиться к выбранному событию и исправлению"
                )
        if (
            context.recheck_conflict_id is not None
            and operation.event_id != context.recheck_event_id
        ):
            raise AnalysisValidationError(
                "Повторная проверка относится только к выбранному конфликту"
            )
        memory = events.get(operation.event_id)
        if memory is None or memory.version != operation.expected_version:
            raise AnalysisValidationError("Неизвестное событие или версия результата анализа")
        if operation.event_id in mutated:
            raise AnalysisValidationError("Для события допустима одна операция в одном ответе")
        mutated.add(operation.event_id)
        evidences = [operation.evidence]
        if isinstance(operation, CreateConflict):
            evidences.extend(v.evidence for v in operation.variants)
        if any(
            corrections[i].event_id != operation.event_id
            for e in evidences
            for i in e.correction_ids
        ):
            raise AnalysisValidationError("Исправление принадлежит другому событию")
        has_new = any(
            e.messages or any(corrections[i].status == "pending" for i in e.correction_ids)
            for e in evidences
        )
        if not has_new and context.recheck_conflict_id is None:
            raise AnalysisValidationError("Изменение требует нового подтверждения")
        if isinstance(operation, UpdateEvent) and operation.event.status != memory.data.status:
            raise AnalysisValidationError("Для отмены/завершения используйте отдельную операцию")


class OperationApplier:
    async def apply(
        self,
        repo: TenantRepository,
        result: AnalysisResult,
        messages: list[MessageData],
        context: AnalysisContext,
    ) -> int:
        validate_result(result, messages, context)
        user = await repo.session.get(User, repo.user_id)
        if user and (user.study_group != context.study_group or user.timezone != context.timezone):
            raise StaleState("Группа или часовой пояс изменились во время анализа")
        # Check every target before writing anything; the caller owns the transaction.
        targets = [o for o in result.operations if isinstance(o, TargetOperation)]
        for target in targets:
            event = await repo.session.scalar(
                select(Event)
                .where(Event.id == target.event_id, Event.user_id == repo.user_id)
                .with_for_update()
            )
            if event is None or event.version != target.expected_version:
                raise StaleState("Событие изменилось во время анализа")
        by_ref = {(m.chat_id, m.message_id): m for m in messages}

        async def resolve(evidence: Evidence) -> list[UUID]:
            ids = list(evidence.source_ids)
            for ref in evidence.messages:
                ids.append((await repo.source(by_ref[(ref.chat_id, ref.message_id)])).id)
            return list(dict.fromkeys(ids))

        created = 0
        for operation in result.operations:
            if isinstance(operation, NoAction):
                continue
            if isinstance(operation, CreateEvent) and expired(
                operation.event, context.timezone, context.current_time
            ):
                due = operation.event.due_at
                if due is None or due.astimezone(ZoneInfo(context.timezone)).date() < (
                    context.current_time.astimezone(ZoneInfo(context.timezone)).date()
                ):
                    continue
            source_ids = await resolve(operation.evidence)
            before = None
            if isinstance(operation, CreateEvent):
                data = operation.event
                from deadline_assistant.repositories.tenant import event_key

                tombstone = await repo.session.scalar(
                    select(EventTombstone.id).where(
                        EventTombstone.user_id == repo.user_id,
                        EventTombstone.dedup_key == event_key(data),
                    )
                )
                if tombstone:
                    continue
                if data.confidence < 0.65:
                    data = data.model_copy(update={"needs_clarification": True})
                duplicate = await repo.duplicate(data)
                event = duplicate or await repo.create_event(data)
                created += int(duplicate is None)
                # Duplicate identity with differing conditions is a conflict, not data loss.
                comparable = event_data(event).model_dump(exclude={"confidence"})
                proposed = data.model_dump(exclude={"confidence"})
                if duplicate and comparable != proposed and not event.completed_by_user:
                    await repo.create_conflict(event.id, data, source_ids)
                    continue
            else:
                event = await repo.event(operation.event_id)
                before = event_data(event).model_dump(mode="json")
                if isinstance(operation, CreateConflict):
                    variants = []
                    for variant in operation.variants:
                        variants.append(
                            (
                                variant.event,
                                await resolve(variant.evidence),
                                variant.evidence.correction_ids,
                                variant.label,
                            )
                        )
                    await repo.create_conflict(event.id, variants=variants, reason=operation.reason)
                    for correction_id in {
                        i
                        for evidence in [
                            operation.evidence,
                            *[v.evidence for v in operation.variants],
                        ]
                        for i in evidence.correction_ids
                    }:
                        correction = await repo.correction_by_id(correction_id)
                        if correction.status == "pending":
                            correction.status = "conflict"
                    continue
                if isinstance(operation, UpdateEvent):
                    # Even syntactically valid low-confidence changes require a choice.
                    fields_changed = event_data(event).model_dump(exclude={"confidence"}) != (
                        operation.event.model_dump(exclude={"confidence"})
                    )
                    if fields_changed and operation.event.confidence < 0.65:
                        await repo.create_conflict(
                            event.id,
                            variants=[
                                (operation.event, source_ids, operation.evidence.correction_ids, "")
                            ],
                        )
                        for correction_id in operation.evidence.correction_ids:
                            correction = await repo.correction_by_id(correction_id)
                            if correction.status == "pending":
                                correction.status = "conflict"
                        continue
                    await repo.write_event(event, operation.event)
                elif isinstance(operation, (CancelEvent, CompleteEvent)):
                    status = (
                        EventStatus.CANCELLED
                        if isinstance(operation, CancelEvent)
                        else EventStatus.COMPLETED
                    )
                    await repo.write_event(
                        event, event_data(event).model_copy(update={"status": status})
                    )
                elif not isinstance(operation, AddSource):
                    raise ValueError("Неизвестная операция")
            if isinstance(operation, (UpdateEvent, CancelEvent, CompleteEvent)):
                for conflict in await repo.conflicts():
                    if conflict.event_id == event.id:
                        conflict.status, conflict.resolved_at = "superseded", utcnow()
            for source_id in source_ids:
                await repo.attach_source(event.id, source_id)
            for correction_id in operation.evidence.correction_ids:
                correction = await repo.correction_by_id(correction_id)
                if correction.status == "pending":
                    correction.previous_data = before
                    correction.applied_data = event_data(event).model_dump(mode="json")
                    correction.status, correction.applied_at = "applied", utcnow()
        return created
