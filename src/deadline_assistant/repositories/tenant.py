import hashlib
import json
from datetime import datetime, timedelta
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from deadline_assistant.db.models import (
    Conflict,
    ConflictOption,
    ConflictOptionSource,
    Event,
    EventList,
    EventSource,
    ProcessedMessage,
    SourceMessage,
    TelegramSession,
    TrackedChat,
    UserCorrection,
)
from deadline_assistant.domain import (
    EntityNotFound,
    EventData,
    EventStatus,
    IntegrationUnavailable,
    MessageData,
    StaleState,
    utcnow,
)


def event_data(event: Event) -> EventData:
    return EventData.model_validate({key: getattr(event, key) for key in EventData.model_fields})


def retention_deadline(data: EventData) -> datetime | None:
    from deadline_assistant.services.lifecycle import boundary

    end = data.resubmission_end
    if end and data.resubmission_precision == "day":
        end = boundary(end, "day", data.timezone)
    return end


def event_key(data: EventData) -> str:
    # Only deterministic identity deduplication; semantic linking belongs to the LLM.
    identity = data.model_dump(
        mode="json", include={"title", "subject", "event_type", "due_at", "schedule_kind"}
    )
    for key in ("title", "subject"):
        identity[key] = " ".join((identity[key] or "").casefold().split())
    return hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()


def source_message(source: SourceMessage) -> MessageData:
    return MessageData(
        chat_id=source.source_chat_id,
        chat_title=source.chat_title,
        message_id=source.source_message_id,
        text=source.original_text or "",
        timestamp=source.message_timestamp,
        edit_timestamp=source.edit_timestamp,
        author_id=source.author_metadata.get("id"),
        author_name=source.author_metadata.get("name"),
        author_role=source.author_metadata.get("role", "unknown"),
        reply_to_message_id=source.reply_to_message_id,
        source_url=source.source_url,
    )


def message_revision(message: MessageData) -> str:
    return hashlib.sha256(message.model_dump_json().encode()).hexdigest()


class TenantRepository:
    def __init__(self, session: AsyncSession, user_id: UUID) -> None:
        self.session = session
        self.user_id = user_id

    async def event(self, event_id: UUID) -> Event:
        event = await self.session.scalar(
            select(Event).where(Event.id == event_id, Event.user_id == self.user_id)
        )
        if event is None:
            raise EntityNotFound
        return event

    async def active_events(self, limit: int | None = None) -> list[Event]:
        return list(
            await self.session.scalars(
                select(Event)
                .where(
                    Event.user_id == self.user_id,
                    Event.status == "active",
                    Event.recurring_deadline_id.is_(None),
                )
                .order_by(Event.due_at.asc().nulls_last(), Event.created_at)
                .limit(limit)
            )
        )

    async def events_with_corrections(self) -> list[Event]:
        corrected = select(UserCorrection.event_id).where(
            UserCorrection.user_id == self.user_id,
            UserCorrection.status.in_(["pending", "conflict", "applied"]),
        )
        return list(
            await self.session.scalars(
                select(Event)
                .where(
                    Event.user_id == self.user_id,
                    Event.id.in_(corrected),
                )
                .order_by(Event.due_at.asc().nulls_last(), Event.created_at)
            )
        )

    async def create_event(self, data: EventData) -> Event:
        event = Event(
            user_id=self.user_id,
            dedup_key=event_key(data),
            retain_until=retention_deadline(data),
            **data.model_dump(),
        )
        self.session.add(event)
        await self.session.flush()
        return event

    async def complete(self, event_id: UUID) -> Event:
        event = await self.event(event_id)
        if event.recurring_deadline_id is not None:
            from deadline_assistant.services.recurring import RecurringService

            return await RecurringService(self.session, self.user_id).close(event.id, event.version)
        event.completed_by_user = True
        await self.write_event(
            event, event_data(event).model_copy(update={"status": EventStatus.COMPLETED})
        )
        return event

    async def correction(
        self, event_id: UUID, text: str, chat_id: int | None = None, message_id: int | None = None
    ) -> UserCorrection:
        event = await self.event(event_id)
        if event.recurring_deadline_id is not None:
            raise ValueError("Измените повторение в меню «Циклические дедлайны».")
        if not text.strip() or len(text) > 6000:
            raise ValueError("Исправление должно содержать от 1 до 6000 символов")
        if chat_id is not None and message_id is not None:
            existing = await self.session.scalar(
                select(UserCorrection).where(
                    UserCorrection.user_id == self.user_id,
                    UserCorrection.telegram_chat_id == chat_id,
                    UserCorrection.telegram_message_id == message_id,
                )
            )
            if existing is not None:
                return existing
        item = UserCorrection(
            user_id=self.user_id,
            event_id=event_id,
            original_text=text,
            telegram_chat_id=chat_id,
            telegram_message_id=message_id,
        )
        self.session.add(item)
        await self.session.flush()
        return item

    async def apply_correction(self, correction_id: UUID, data: EventData) -> None:
        correction = await self.session.scalar(
            select(UserCorrection).where(
                UserCorrection.id == correction_id, UserCorrection.user_id == self.user_id
            )
        )
        if correction is None:
            raise EntityNotFound
        if correction.status != "pending":
            raise ValueError("Исправление уже обработано")
        event = await self.event(correction.event_id)
        correction.previous_data = event_data(event).model_dump(mode="json")
        await self.write_event(event, data)
        correction.applied_data = event_data(event).model_dump(mode="json")
        correction.status = "applied"
        correction.applied_at = utcnow()

    async def chats(self) -> list[TrackedChat]:
        return list(
            await self.session.scalars(
                select(TrackedChat).where(
                    TrackedChat.user_id == self.user_id, TrackedChat.enabled.is_(True)
                )
            )
        )

    async def chat(self, chat_id: UUID) -> TrackedChat:
        chat = await self.session.scalar(
            select(TrackedChat).where(
                TrackedChat.id == chat_id, TrackedChat.user_id == self.user_id
            )
        )
        if chat is None:
            raise EntityNotFound
        return chat

    async def track(
        self, telegram_chat_id: int, title: str, metadata: dict[str, int | str]
    ) -> TrackedChat:
        chat = await self.session.scalar(
            select(TrackedChat).where(
                TrackedChat.user_id == self.user_id,
                TrackedChat.telegram_chat_id == telegram_chat_id,
            )
        )
        if chat is None:
            chat = TrackedChat(
                user_id=self.user_id,
                telegram_chat_id=telegram_chat_id,
                title=title[:300],
                peer_metadata=metadata,
            )
            self.session.add(chat)
        else:
            chat.enabled = True
            chat.title = title[:300]
            chat.peer_metadata = metadata
        await self.session.flush()
        return chat

    async def telegram_session(self) -> TelegramSession | None:
        return await self.session.scalar(
            select(TelegramSession).where(TelegramSession.user_id == self.user_id)
        )

    async def require_connection(self) -> TelegramSession:
        credential = await self.telegram_session()
        if credential is None or credential.status != "connected":
            raise IntegrationUnavailable("Сначала подключите аккаунт: /connect")
        return credential

    async def save_session(self, encrypted: str) -> TelegramSession:
        item = await self.telegram_session()
        if item is None:
            item = TelegramSession(user_id=self.user_id, encrypted_session_data=encrypted)
            self.session.add(item)
        else:
            item.encrypted_session_data = encrypted
            item.status = "connected"
            item.connected_at = utcnow()
            item.last_error = None
        await self.session.flush()
        return item

    async def is_processed(self, message: MessageData) -> bool:
        item = await self.session.get(
            ProcessedMessage, (self.user_id, message.chat_id, message.message_id)
        )
        return item is not None and item.revision == message_revision(message)

    async def mark_processed(self, message: MessageData) -> None:
        item = await self.session.get(
            ProcessedMessage, (self.user_id, message.chat_id, message.message_id)
        )
        if item is None:
            item = ProcessedMessage(
                user_id=self.user_id, chat_id=message.chat_id, message_id=message.message_id
            )
            self.session.add(item)
        item.edit_timestamp = message.edit_timestamp
        item.revision = message_revision(message)
        item.processed_at = utcnow()

    async def source(self, message: MessageData) -> SourceMessage:
        revision = message_revision(message)
        source = await self.session.scalar(
            select(SourceMessage).where(
                SourceMessage.user_id == self.user_id,
                SourceMessage.source_chat_id == message.chat_id,
                SourceMessage.source_message_id == message.message_id,
                SourceMessage.revision == revision,
            )
        )
        if source is None:
            source = SourceMessage(
                user_id=self.user_id,
                source_chat_id=message.chat_id,
                source_message_id=message.message_id,
                revision=revision,
                original_text=message.text,
                message_timestamp=message.timestamp,
                edit_timestamp=message.edit_timestamp,
                author_metadata={
                    "id": message.author_id,
                    "name": message.author_name,
                    "role": message.author_role,
                },
                source_url=message.source_url,
                chat_title=message.chat_title,
                reply_to_message_id=message.reply_to_message_id,
            )
            self.session.add(source)
            await self.session.flush()
        return source

    async def attach_source(self, event_id: UUID, source_id: UUID) -> None:
        event = await self.event(event_id)
        source = await self.session.scalar(
            select(SourceMessage).where(
                SourceMessage.id == source_id, SourceMessage.user_id == self.user_id
            )
        )
        if source is None:
            raise EntityNotFound
        exists = await self.session.scalar(
            select(EventSource.id).where(
                EventSource.user_id == self.user_id,
                EventSource.event_id == event_id,
                EventSource.source_id == source_id,
            )
        )
        if exists is None:
            event.version += 1
            self.session.add(
                EventSource(user_id=self.user_id, event_id=event_id, source_id=source_id)
            )

    async def sources(
        self, event_id: UUID, *, include_conflicts: bool = False
    ) -> list[SourceMessage]:
        await self.event(event_id)
        items = list(
            await self.session.scalars(
                select(SourceMessage)
                .join(EventSource, EventSource.source_id == SourceMessage.id)
                .where(
                    EventSource.user_id == self.user_id,
                    EventSource.event_id == event_id,
                    SourceMessage.user_id == self.user_id,
                )
            )
        )
        if include_conflicts:
            extra = await self.session.scalars(
                select(SourceMessage)
                .join(ConflictOptionSource, ConflictOptionSource.source_id == SourceMessage.id)
                .join(ConflictOption, ConflictOption.id == ConflictOptionSource.option_id)
                .join(Conflict, Conflict.id == ConflictOption.conflict_id)
                .where(
                    SourceMessage.user_id == self.user_id,
                    ConflictOptionSource.user_id == self.user_id,
                    Conflict.user_id == self.user_id,
                    Conflict.event_id == event_id,
                )
            )
            items = list({s.id: s for s in [*items, *extra]}.values())
        return items

    async def current_sources(self, event_id: UUID) -> list[SourceMessage]:
        latest: dict[tuple[int, int], SourceMessage] = {}
        for source in sorted(
            await self.sources(event_id), key=lambda s: s.edit_timestamp or s.message_timestamp
        ):
            latest[(source.source_chat_id, source.source_message_id)] = source
        return list(latest.values())

    async def write_event(self, event: Event, data: EventData) -> None:
        if event.user_id != self.user_id:
            raise EntityNotFound
        if event.recurring_deadline_id is not None:
            raise ValueError("Измените повторение в меню «Циклические дедлайны».")
        if event.completed_by_user:
            data = data.model_copy(update={"status": EventStatus.COMPLETED})
        if event_data(event) == data:
            return
        for key, value in data.model_dump().items():
            setattr(event, key, value)
        event.dedup_key = event_key(data)
        if data.status == "active":
            event.completed_at = None
        elif event.completed_at is None:
            event.completed_at = utcnow()
        event.retain_until = retention_deadline(data)
        # Optimistic ORM versioning covers concurrent transactions on PostgreSQL too.
        await self.session.flush()

    async def duplicate(self, data: EventData) -> Event | None:
        candidates = list(
            await self.session.scalars(
                select(Event).where(
                    Event.user_id == self.user_id,
                    Event.recurring_deadline_id.is_(None),
                    (Event.dedup_key == event_key(data)) | Event.dedup_key.is_(None),
                )
            )
        )
        return next((e for e in candidates if event_key(event_data(e)) == event_key(data)), None)

    async def correction_by_id(self, correction_id: UUID) -> UserCorrection:
        correction = await self.session.scalar(
            select(UserCorrection).where(
                UserCorrection.user_id == self.user_id, UserCorrection.id == correction_id
            )
        )
        if correction is None:
            raise EntityNotFound
        return correction

    async def save_event_list(self, events: list[Event]) -> EventList:
        listing = EventList(
            user_id=self.user_id,
            entries=[{"id": str(e.id), "version": e.version} for e in events],
            expires_at=utcnow() + timedelta(hours=24),
        )
        self.session.add(listing)
        await self.session.flush()
        return listing

    async def event_list(self, list_id: UUID) -> EventList:
        listing = await self.session.scalar(
            select(EventList).where(
                EventList.id == list_id,
                EventList.user_id == self.user_id,
                EventList.expires_at > utcnow(),
            )
        )
        if listing is None:
            raise StaleState("Список устарел. Откройте новый список.")
        return listing

    async def listed_event(
        self, list_id: UUID, event_id: UUID, *, require_active: bool = True
    ) -> Event:
        listing = await self.event_list(list_id)
        entry = next((e for e in listing.entries if e["id"] == str(event_id)), None)
        if entry is None:
            raise EntityNotFound
        event = await self.event(event_id)
        if event.version != entry["version"] or (require_active and event.status != "active"):
            raise StaleState("Событие изменилось. Откройте новый список.")
        return event

    async def undo_correction(self, event_id: UUID) -> None:
        event = await self.event(event_id)
        correction = await self.session.scalar(
            select(UserCorrection)
            .where(
                UserCorrection.user_id == self.user_id,
                UserCorrection.event_id == event_id,
                UserCorrection.status.in_(["pending", "conflict", "applied"]),
            )
            .order_by(UserCorrection.created_at.desc(), UserCorrection.id.desc())
            .limit(1)
        )
        if correction is None:
            raise ValueError("У задания нет исправлений для отмены")
        if correction.status == "applied":
            if not correction.previous_data or not correction.applied_data:
                raise StaleState("Для старого исправления нет сохранённой предыдущей версии.")
            current = event_data(event).model_dump(mode="json", exclude={"status"})
            applied = EventData.model_validate(correction.applied_data).model_dump(
                mode="json", exclude={"status"}
            )
            if current != applied:
                raise StaleState("После исправления появились новые изменения. Отмена их затронет.")
            previous = EventData.model_validate(correction.previous_data)
            await self.write_event(
                event, previous.model_copy(update={"status": event_data(event).status})
            )
        correction.status = "reverted"
        # In-flight model results can no longer apply a withdrawn correction.
        event.version += 1
        # Preserve teacher evidence in existing conflicts. Their old event_version now
        # requires an explicit recheck without the withdrawn correction in LLM memory.
        await self.session.flush()

    async def create_conflict(
        self,
        event_id: UUID,
        proposal: EventData | None = None,
        source_ids: list[UUID] | None = None,
        *,
        variants: list[tuple[EventData, list[UUID], list[UUID], str]] | None = None,
        reason: str = "Противоречащие источники",
    ) -> Conflict:
        event = await self.event(event_id)
        if variants is None:
            if proposal is None:
                raise ValueError("Нет вариантов конфликта")
            variants = [(proposal, source_ids or [], [], "")]
        current_sources = [s.id for s in await self.current_sources(event_id)]
        latest_correction = await self.session.scalar(
            select(UserCorrection)
            .where(
                UserCorrection.user_id == self.user_id,
                UserCorrection.event_id == event_id,
                UserCorrection.status == "applied",
            )
            .order_by(UserCorrection.created_at.desc())
            .limit(1)
        )
        current_corrections = [latest_correction.id] if latest_correction else []
        variants = [
            (event_data(event), current_sources, current_corrections, "Текущее состояние"),
            *variants,
        ]
        conflicts = await self.conflicts()
        conflict = next((c for c in conflicts if c.event_id == event_id), None)
        if conflict is None:
            conflict = Conflict(user_id=self.user_id, event_id=event_id, reason=reason)
            self.session.add(conflict)
            await self.session.flush()
        elif conflict.event_version != event.version:
            # Never silently rebase prepared options over newer manual changes.
            conflict.status = "superseded"
            conflict.resolved_at = utcnow()
            conflict = Conflict(user_id=self.user_id, event_id=event_id, reason=reason)
            self.session.add(conflict)
            await self.session.flush()
        options = await self.conflict_options(conflict.id)
        changed = not options
        for data, ids, correction_ids, label in variants:
            for correction_id in correction_ids:
                if (await self.correction_by_id(correction_id)).event_id != event_id:
                    raise EntityNotFound
            serialized = data.model_dump(mode="json")
            option = next(
                (
                    o
                    for o in options
                    if EventData.model_validate(o.proposed_event_data).model_dump(
                        exclude={"confidence"}
                    )
                    == data.model_dump(exclude={"confidence"})
                ),
                None,
            )
            if option is None:
                option = ConflictOption(
                    user_id=self.user_id,
                    conflict_id=conflict.id,
                    option_number=len(options) + 1,
                    proposed_event_data=serialized,
                    correction_ids=[str(i) for i in correction_ids],
                    label=label,
                )
                self.session.add(option)
                await self.session.flush()
                options.append(option)
                changed = True
            existing_ids = set(
                await self.session.scalars(
                    select(ConflictOptionSource.source_id).where(
                        ConflictOptionSource.user_id == self.user_id,
                        ConflictOptionSource.option_id == option.id,
                    )
                )
            )
            for source_id in set(ids) - existing_ids:
                source = await self.session.scalar(
                    select(SourceMessage.id).where(
                        SourceMessage.id == source_id, SourceMessage.user_id == self.user_id
                    )
                )
                if source is None:
                    raise EntityNotFound
                self.session.add(
                    ConflictOptionSource(
                        user_id=self.user_id, option_id=option.id, source_id=source_id
                    )
                )
                changed = True
            merged = sorted(set(option.correction_ids) | {str(i) for i in correction_ids})
            if merged != option.correction_ids:
                option.correction_ids = merged
                changed = True
        if len(options) < 2:
            raise ValueError("Конфликт требует разных вариантов")
        if changed:
            event.version += 1
        conflict.event_version = event.version
        conflict.reason = reason
        await self.session.flush()
        return conflict

    async def conflicts(self) -> list[Conflict]:
        return list(
            await self.session.scalars(
                select(Conflict)
                .where(
                    Conflict.user_id == self.user_id, Conflict.status.in_(["open", "unconfirmed"])
                )
                .order_by(Conflict.created_at)
            )
        )

    async def conflict(self, conflict_id: UUID) -> Conflict:
        conflict = await self.session.scalar(
            select(Conflict)
            .where(
                Conflict.id == conflict_id,
                Conflict.user_id == self.user_id,
                Conflict.status.in_(["open", "unconfirmed"]),
            )
            .with_for_update()
        )
        if conflict is None:
            raise EntityNotFound
        return conflict

    async def conflict_options(self, conflict_id: UUID) -> list[ConflictOption]:
        await self.conflict(conflict_id)
        return list(
            await self.session.scalars(
                select(ConflictOption)
                .where(
                    ConflictOption.conflict_id == conflict_id,
                    ConflictOption.user_id == self.user_id,
                )
                .order_by(ConflictOption.option_number)
            )
        )

    async def resolve_conflict(
        self, conflict_id: UUID, number: int, expected_version: int | None = None
    ) -> None:
        conflict = await self.conflict(conflict_id)
        event = await self.session.scalar(
            select(Event)
            .where(Event.id == conflict.event_id, Event.user_id == self.user_id)
            .with_for_update()
        )
        if event is None:
            raise EntityNotFound
        if conflict.event_version != event.version or (
            expected_version is not None and expected_version != conflict.event_version
        ):
            raise StaleState("Событие изменилось. Повторно проверьте конфликт.")
        if number == 0:
            conflict.status = "unconfirmed"
            event.version += 1
            conflict.event_version = event.version
            await self.session.flush()
            return
        options = await self.conflict_options(conflict.id)
        option = next((o for o in options if o.option_number == number), None)
        if option is None:
            raise EntityNotFound
        data = EventData.model_validate(option.proposed_event_data)
        before = event_data(event).model_dump(mode="json")
        await self.write_event(event, data)
        sources = list(
            await self.session.scalars(
                select(ConflictOptionSource.source_id).where(
                    ConflictOptionSource.option_id == option.id,
                    ConflictOptionSource.user_id == self.user_id,
                )
            )
        )
        for source_id in sources:
            await self.attach_source(event.id, source_id)
        # Corrections considered in every variant are resolved alongside the choice.
        all_corrections = {UUID(i) for o in options for i in o.correction_ids}
        for correction_id in all_corrections:
            correction = await self.correction_by_id(correction_id)
            if correction.event_id != event.id:
                raise EntityNotFound
            if correction.status in {"pending", "conflict"}:
                correction.status = (
                    "applied" if str(correction_id) in option.correction_ids else "rejected"
                )
                correction.applied_at = utcnow()
                if correction.status == "applied":
                    correction.previous_data = before
                    correction.applied_data = event_data(event).model_dump(mode="json")
        conflict.status = "resolved"
        conflict.resolved_at = utcnow()
        await self.session.flush()
