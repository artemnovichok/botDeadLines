"""Calendar-day recurrences, shared by the bot, CLI and scheduled digests."""

from datetime import UTC, date, datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deadline_assistant.db.models import Event, RecurringDeadline
from deadline_assistant.domain import EntityNotFound, EventData, StaleState, utcnow

MSK = "Europe/Moscow"


def resolve_first_due(
    day: int | date,
    due_time: time | None,
    timezone: str,
    *,
    now: datetime | None = None,
) -> datetime:
    now = now or utcnow()
    zone = ZoneInfo(timezone if due_time is not None else MSK)
    local = now.astimezone(zone)
    due_time = due_time if due_time is not None else time(23, 59)
    if due_time.tzinfo is not None or due_time.second or due_time.microsecond:
        raise ValueError("Введите время в формате ЧЧ:ММ.")
    if isinstance(day, int):
        if not 0 <= day <= 6:
            raise ValueError("Выберите день недели.")
        target = local.date() + timedelta(days=(day - local.weekday()) % 7)
    else:
        target = day
    result = datetime.combine(target, due_time, zone).astimezone(UTC)
    if result < now:
        if isinstance(day, int):
            result = datetime.combine(target + timedelta(days=7), due_time, zone).astimezone(UTC)
        else:
            raise ValueError("Первый срок уже прошёл. Укажите ближайшую будущую дату и время.")
    return result


def next_due(first_due_at: datetime, interval_days: int, timezone: str, now: datetime) -> datetime:
    if not 1 <= interval_days <= 14:
        raise ValueError("Интервал должен быть от 1 до 14 дней.")
    zone = ZoneInfo(timezone)
    first = first_due_at.astimezone(zone)
    local_now = now.astimezone(zone)
    steps = max(0, (local_now.date() - first.date()).days // interval_days)
    candidate = first + timedelta(days=steps * interval_days)
    if candidate.astimezone(UTC) < now:
        candidate += timedelta(days=interval_days)
    return candidate.astimezone(UTC)


class RecurringService:
    def __init__(self, session: AsyncSession, user_id: UUID) -> None:
        self.session = session
        self.user_id = user_id

    async def get(self, series_id: UUID) -> RecurringDeadline:
        series = await self.session.scalar(
            select(RecurringDeadline)
            .where(RecurringDeadline.id == series_id, RecurringDeadline.user_id == self.user_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if series is None:
            raise EntityNotFound
        return series

    async def list_active(self) -> list[RecurringDeadline]:
        return list(
            await self.session.scalars(
                select(RecurringDeadline)
                .where(
                    RecurringDeadline.user_id == self.user_id,
                    RecurringDeadline.enabled.is_(True),
                )
                .order_by(RecurringDeadline.created_at, RecurringDeadline.id)
                .with_for_update()
            )
        )

    @staticmethod
    def _validate(
        title: str,
        interval_days: int,
        first_due_at: datetime,
        timezone: str,
        has_time: bool,
        now: datetime,
    ) -> EventData:
        if type(interval_days) is not int or not 1 <= interval_days <= 14:
            raise ValueError("Интервал должен быть от 1 до 14 дней.")
        data = EventData(
            title=title,
            due_at=first_due_at,
            timezone=timezone,
            date_precision="exact",
            confidence=1.0,
        )
        if first_due_at < now:
            raise ValueError("Первый срок уже прошёл. Выберите ближайший будущий срок.")
        if not has_time:
            local = first_due_at.astimezone(ZoneInfo(MSK))
            if timezone != MSK or local.time() != time(23, 59):
                raise ValueError("Без времени — до 23:59 МСК выбранного дня.")
        return data

    async def create(
        self,
        title: str,
        interval_days: int,
        first_due_at: datetime,
        timezone: str,
        has_time: bool,
        *,
        series_id: UUID | None = None,
        now: datetime | None = None,
    ) -> RecurringDeadline:
        if series_id is not None:
            existing = await self.session.get(RecurringDeadline, series_id)
            if existing is not None:
                if existing.user_id != self.user_id:
                    raise EntityNotFound
                return existing
        data = self._validate(
            title, interval_days, first_due_at, timezone, has_time, now or utcnow()
        )
        series = RecurringDeadline(
            id=series_id or uuid4(),
            user_id=self.user_id,
            title=data.title,
            interval_days=interval_days,
            first_due_at=data.due_at,
            timezone=timezone,
            has_time=has_time,
        )
        try:
            async with self.session.begin_nested():
                self.session.add(series)
                await self.session.flush()
        except IntegrityError:
            if series_id is None:
                raise
            # Another request may have saved the same draft while this one waited.
            return await self.get(series_id)
        return series

    async def _cancel_events(self, series_id: UUID) -> None:
        for event in await self.session.scalars(
            select(Event).where(
                Event.user_id == self.user_id,
                Event.recurring_deadline_id == series_id,
                Event.status != "cancelled",
            )
        ):
            event.status = "cancelled"
            event.completed_by_user = False
            event.completed_at = utcnow()

    async def update(
        self,
        series_id: UUID,
        expected_version: int,
        *,
        title: str,
        interval_days: int,
        first_due_at: datetime,
        timezone: str,
        has_time: bool,
        now: datetime | None = None,
    ) -> RecurringDeadline:
        series = await self.get(series_id)
        if not series.enabled or series.version != expected_version:
            raise StaleState("Расписание изменилось. Откройте циклический дедлайн заново.")
        data = self._validate(
            title, interval_days, first_due_at, timezone, has_time, now or utcnow()
        )
        await self._cancel_events(series_id)
        series.title = data.title
        series.interval_days = interval_days
        series.first_due_at = first_due_at
        series.timezone = timezone
        series.has_time = has_time
        # Even an identical edit invalidates old confirmation/card buttons.
        series.version += 1
        await self.session.flush()
        return series

    async def _current(self, series: RecurringDeadline, now: datetime) -> Event:
        if not series.enabled:
            raise StaleState("Отслеживание этого дедлайна закончено.")
        due = next_due(series.first_due_at, series.interval_days, series.timezone, now)
        event = await self.session.scalar(
            select(Event).where(
                Event.user_id == self.user_id,
                Event.recurring_deadline_id == series.id,
                Event.due_at == due,
            )
        )
        if event is None or event.status == "cancelled":
            data = EventData(
                title=series.title,
                due_at=due,
                timezone=series.timezone,
                date_precision="exact",
                confidence=1.0,
            )
            if event is None:
                event = Event(
                    user_id=self.user_id, recurring_deadline_id=series.id, **data.model_dump()
                )
                self.session.add(event)
            else:
                for key, value in data.model_dump().items():
                    setattr(event, key, value)
                event.completed_by_user = False
                event.completed_at = None
            await self.session.flush()
        return event

    async def current(self, series_id: UUID, *, now: datetime | None = None) -> Event:
        return await self._current(await self.get(series_id), now or utcnow())

    async def current_events(self, *, now: datetime | None = None) -> list[Event]:
        point = now or utcnow()
        return [await self._current(series, point) for series in await self.list_active()]

    async def close(
        self, event_id: UUID, expected_version: int, *, now: datetime | None = None
    ) -> Event:
        event = await self.session.scalar(
            select(Event).where(Event.id == event_id, Event.user_id == self.user_id)
        )
        if event is None or event.recurring_deadline_id is None:
            raise EntityNotFound
        series = await self.get(event.recurring_deadline_id)
        # A second request may have read the event before waiting for the series
        # lock. Refresh it after acquiring that lock to reject an old card.
        await self.session.refresh(event)
        point = now or utcnow()
        if (
            not series.enabled
            or event.version != expected_version
            or event.due_at
            != next_due(series.first_due_at, series.interval_days, series.timezone, point)
            or event.status != "active"
        ):
            raise StaleState("Это повторение уже изменилось. Откройте актуальный дедлайн.")
        event.status = "completed"
        event.completed_by_user = True
        event.completed_at = point
        await self.session.flush()
        return event

    async def stop(self, series_id: UUID, expected_version: int) -> RecurringDeadline:
        series = await self.get(series_id)
        if not series.enabled or series.version != expected_version:
            raise StaleState("Расписание изменилось. Откройте циклический дедлайн заново.")
        series.enabled = False
        await self._cancel_events(series_id)
        await self.session.flush()
        return series
