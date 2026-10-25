from datetime import datetime, timedelta

from sqlalchemy import delete, exists, or_, select, update

from deadline_assistant.config import Settings
from deadline_assistant.db.models import (
    AnalysisBatch,
    AnalysisRun,
    AuthLink,
    CallbackReceipt,
    Conflict,
    ConflictOption,
    ConflictOptionSource,
    Event,
    EventList,
    EventSource,
    EventTombstone,
    ProcessedMessage,
    RequestBudget,
    ScheduledDelivery,
    SourceMessage,
    TrackedChat,
    User,
    UserCorrection,
)
from deadline_assistant.db.session import Database
from deadline_assistant.domain import utcnow
from deadline_assistant.repositories.tenant import event_data, event_key
from deadline_assistant.services.lifecycle import lifecycle_end


class RetentionService:
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database = database
        self.settings = settings

    async def cleanup(self, now: datetime | None = None) -> int:
        now = now or utcnow()
        cutoff = now - timedelta(days=self.settings.retention_days)
        event_needs_source = exists(
            select(EventSource.id)
            .join(Event, Event.id == EventSource.event_id)
            .where(
                EventSource.source_id == SourceMessage.id,
                or_(
                    Event.status == "active",
                    Event.completed_at > cutoff,
                    Event.retain_until > now,
                    Event.resubmission_end > now,
                    # У закрытого события без даты завершения срок ещё нельзя вычислить.
                    Event.completed_at.is_(None),
                ),
            )
        )
        conflict_needs_source = exists(
            select(ConflictOptionSource.option_id)
            .join(ConflictOption, ConflictOption.id == ConflictOptionSource.option_id)
            .join(Conflict, Conflict.id == ConflictOption.conflict_id)
            .where(
                ConflictOptionSource.source_id == SourceMessage.id,
                Conflict.status.in_(["open", "unconfirmed"]),
            )
        )
        async with self.database.transaction() as session:
            protected = set(
                await session.scalars(
                    select(Conflict.event_id).where(Conflict.status.in_(["open", "unconfirmed"]))
                )
            )
            protected.update(
                await session.scalars(
                    select(UserCorrection.event_id).where(
                        UserCorrection.status.in_(["pending", "conflict"])
                    )
                )
            )
            zones = dict((await session.execute(select(User.id, User.timezone))).all())
            for event in list(await session.scalars(select(Event))):
                if event.id in protected:
                    continue
                end = lifecycle_end(event_data(event), zones[event.user_id])
                if event.retain_until and event.retain_until > now:
                    continue
                if end and end > now:
                    continue
                if event.status == "active":
                    remove = not event.needs_clarification and end is not None and end < cutoff
                else:
                    remove = event.completed_at is not None and event.completed_at < cutoff
                if remove:
                    if event.recurring_deadline_id is not None:
                        await session.delete(event)
                        continue
                    key = event_key(event_data(event))
                    existing = await session.scalar(
                        select(EventTombstone.id).where(
                            EventTombstone.user_id == event.user_id, EventTombstone.dedup_key == key
                        )
                    )
                    if existing is None:
                        session.add(EventTombstone(user_id=event.user_id, dedup_key=key))
                    await session.delete(event)
            await session.flush()
            # Метаданные и связи остаются; удаляем только ненужные тексты.
            result = await session.execute(
                update(SourceMessage)
                .where(
                    SourceMessage.original_text.is_not(None),
                    ~event_needs_source,
                    ~conflict_needs_source,
                )
                .values(original_text=None)
            )
            chats = list(await session.scalars(select(TrackedChat)))
            for chat in chats:
                threshold = await session.scalar(
                    select(ProcessedMessage.message_id)
                    .where(
                        ProcessedMessage.user_id == chat.user_id,
                        ProcessedMessage.chat_id == chat.telegram_chat_id,
                    )
                    .order_by(ProcessedMessage.message_id.desc())
                    .offset(self.settings.edit_lookback_limit + self.settings.sync_batch_size - 1)
                    .limit(1)
                )
                if threshold is not None:
                    await session.execute(
                        delete(ProcessedMessage).where(
                            ProcessedMessage.user_id == chat.user_id,
                            ProcessedMessage.chat_id == chat.telegram_chat_id,
                            ProcessedMessage.message_id < threshold,
                            ProcessedMessage.processed_at < cutoff,
                        )
                    )
            await session.execute(delete(AuthLink).where(AuthLink.expires_at < now))
            await session.execute(delete(AnalysisBatch).where(AnalysisBatch.created_at < cutoff))
            await session.execute(delete(RequestBudget).where(RequestBudget.next_at < cutoff))
            await session.execute(delete(EventList).where(EventList.expires_at < now))
            await session.execute(
                delete(CallbackReceipt).where(CallbackReceipt.created_at < cutoff)
            )
            await session.execute(
                delete(AnalysisRun).where(
                    AnalysisRun.started_at < cutoff,
                    AnalysisRun.status.notin_(["running", "queued"]),
                )
            )
            await session.execute(
                delete(ScheduledDelivery).where(ScheduledDelivery.local_date < cutoff.date())
            )
            return int(getattr(result, "rowcount", 0))
