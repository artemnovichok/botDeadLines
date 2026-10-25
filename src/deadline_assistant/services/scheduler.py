import asyncio
from collections.abc import Awaitable, Callable
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import structlog
from aiogram.exceptions import TelegramForbiddenError, TelegramRetryAfter
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from sqlalchemy import select

from deadline_assistant.config import Settings
from deadline_assistant.db.models import ScheduledDelivery, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import DeliveryBlocked, utcnow
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.coordination import JobBusy
from deadline_assistant.services.cycle import CycleService
from deadline_assistant.services.delivery import SummaryDelivery
from deadline_assistant.services.interfaces import Notifier
from deadline_assistant.services.retention import RetentionService
from deadline_assistant.services.sync import SyncService

log = structlog.get_logger()


class SchedulerService:
    """Durable local-date scheduling shared safely with CLI processes."""

    def __init__(
        self,
        database: Database,
        settings: Settings,
        sync: SyncService,
        notifier: Notifier | None,
        retention: RetentionService,
    ) -> None:
        self.database, self.settings, self.sync = database, settings, sync
        self.notifier, self.retention = notifier, retention
        self.scheduler = AsyncIOScheduler(timezone="UTC")
        self.tick_lock = asyncio.Lock()
        self.maintenance_lock = asyncio.Lock()
        self.stopping = False
        self.cycle = CycleService(database, settings, sync, notifier)
        self.summary = SummaryDelivery(database, notifier)

    async def start(self) -> None:
        self.scheduler.add_job(
            self.tick,
            "interval",
            seconds=60,
            id="daily_jobs",
            max_instances=1,
            coalesce=True,
            misfire_grace_time=120,
            next_run_time=utcnow(),
        )
        self.scheduler.add_job(
            self.cleanup,
            "interval",
            hours=24,
            id="retention",
            max_instances=1,
            coalesce=True,
            next_run_time=utcnow(),
        )
        self.scheduler.start()

    async def stop(self) -> None:
        self.stopping = True
        self.scheduler.pause()
        # Ждём выполняющийся tick, затем останавливаем APScheduler до закрытия БД.
        async with self.tick_lock, self.maintenance_lock:
            self.scheduler.shutdown(wait=False)
        await asyncio.sleep(0)

    async def cleanup(self) -> None:
        async with self.maintenance_lock:
            try:
                await self.retention.cleanup()
            except Exception as exc:
                log.error("Ошибка очистки данных", error_type=type(exc).__name__)

    async def maintenance(self, action: Callable[[], Awaitable[None]]) -> None:
        async with self.maintenance_lock:
            try:
                await action()
            except Exception as exc:
                log.error("Ошибка обслуживания", error_type=type(exc).__name__)

    async def tick(self, now: datetime | None = None) -> None:
        async with self.tick_lock:
            now = now or utcnow()
            async with self.database.transaction() as session:
                users = await UserRepository(session).enabled()
            semaphore = asyncio.Semaphore(self.settings.cycle_concurrency)

            async def process(user: User) -> None:
                async with semaphore:
                    try:
                        await self._tick_user(user, now)
                    except Exception as exc:
                        log.warning(
                            "Ошибка расписания пользователя",
                            user_id=str(user.id),
                            error_type=type(exc).__name__,
                        )

            await asyncio.gather(*(process(user) for user in users))

    async def _tick_user(self, user: User, now: datetime) -> None:
        if self.stopping or not user.study_group or user.initial_analysis_choice is None:
            return
        async with self.database.transaction() as session:
            credential = await TenantRepository(session, user.id).telegram_session()
            if credential is None or credential.status != "connected":
                return
        local = now.astimezone(ZoneInfo(user.timezone))
        for kind, due in [
            ("sync", self.settings.night_sync_time),
            ("digest", user.preferred_digest_time),
        ]:
            if kind == "digest" and user.bot_blocked_at:
                continue
            if kind == "sync" and not (
                self.settings.llm_enabled and self.settings.telegram_enabled
            ):
                continue
            if kind == "digest" and self.notifier is None:
                continue
            local_date = local.date()
            if local.time().replace(tzinfo=None) < due:
                if kind == "digest":
                    continue
                local_date -= timedelta(days=1)
            if kind == "sync":
                night_due = datetime.combine(local_date, due, ZoneInfo(user.timezone))
                if now - night_due >= timedelta(hours=1):
                    continue
                async with self.database.transaction() as session:
                    repo = TenantRepository(session, user.id)
                    credential = await repo.telegram_session()
                    if not credential or credential.status != "connected" or not await repo.chats():
                        continue
            await self._deliver(user, kind, now, local_date)

    async def _deliver(
        self, user: User, kind: str, now: datetime, local_date: date | None = None
    ) -> None:
        local_date = local_date or now.astimezone(ZoneInfo(user.timezone)).date()
        try:
            async with self.database.lock(f"delivery:{user.id}:{kind}:{local_date}", wait=False):
                await self._deliver_locked(user, kind, now, local_date)
        except JobBusy:
            return

    async def _deliver_locked(self, user: User, kind: str, now: datetime, local_date: date) -> None:
        async with self.database.transaction() as session:
            stored_user = await UserRepository(session).require(user.id)
            if not stored_user.study_group or stored_user.initial_analysis_choice is None:
                return
            credential = await TenantRepository(session, user.id).telegram_session()
            if credential is None or credential.status != "connected":
                return
            delivery = await session.scalar(
                select(ScheduledDelivery).where(
                    ScheduledDelivery.user_id == user.id,
                    ScheduledDelivery.kind == kind,
                    ScheduledDelivery.local_date == local_date,
                )
            )
            if delivery is None:
                delivery = ScheduledDelivery(
                    user_id=user.id, kind=kind, local_date=local_date, next_attempt_at=now
                )
                session.add(delivery)
                await session.flush()
            if (
                delivery.status in {"sent", "skipped", "blocked", "failed"}
                or delivery.next_attempt_at > now
            ):
                return
            if kind == "digest":
                due = datetime.combine(
                    local_date, user.preferred_digest_time, ZoneInfo(user.timezone)
                )
                if now - due > timedelta(hours=self.settings.digest_catchup_hours):
                    delivery.status, delivery.completed_at = "skipped", now
                    return
            delivery.attempts += 1
            # После аварии повторяем задачу; Telegram не предоставляет idempotency key отправки.
            delivery.next_attempt_at = now + timedelta(
                seconds=min(3600, 60 * 2 ** min(delivery.attempts, 6))
            )
            delivery_id = delivery.id
        try:
            if kind == "sync":
                await self.cycle.run_user(user.id)
                await self.cleanup()
            elif self.notifier:
                await self.summary.deliver(
                    user.id,
                    delivery_id,
                    now=now,
                    stale_reason="Анализ ИИ недоступен. Показаны сохранённые данные."
                    if not self.settings.llm_enabled
                    else None,
                )
            async with self.database.transaction() as session:
                delivery = await session.get(ScheduledDelivery, delivery_id)
                if delivery is None:
                    return
                delivery.status, delivery.completed_at = "sent", utcnow()
        except Exception as exc:
            async with self.database.transaction() as session:
                delivery = await session.get(ScheduledDelivery, delivery_id)
                if delivery is None:
                    return
                credential = await TenantRepository(session, user.id).telegram_session()
                if credential is None or credential.status != "connected":
                    delivery.status, delivery.completed_at = "skipped", now
                elif isinstance(exc, (DeliveryBlocked, TelegramForbiddenError)):
                    delivery.status, delivery.completed_at = "blocked", now
                    stored = await session.get(User, user.id)
                    assert stored is not None
                    stored.bot_blocked_at = now
                elif delivery.attempts >= self.settings.delivery_max_attempts:
                    delivery.status, delivery.completed_at = "failed", now
                elif isinstance(exc, TelegramRetryAfter):
                    delivery.next_attempt_at = now + timedelta(seconds=max(30, exc.retry_after))
            log.warning(
                "Задача будет повторена",
                user_id=str(user.id),
                kind=kind,
                error_type=type(exc).__name__,
            )
