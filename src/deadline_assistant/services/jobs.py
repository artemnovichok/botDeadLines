import asyncio
from uuid import UUID
from zoneinfo import ZoneInfo

import structlog
from sqlalchemy import select, update

from deadline_assistant.db.models import AnalysisRun
from deadline_assistant.db.session import Database
from deadline_assistant.domain import IntegrationUnavailable, utcnow
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.coordination import JobBusy
from deadline_assistant.services.delivery import SummaryDelivery
from deadline_assistant.services.interfaces import Notifier
from deadline_assistant.services.sync import SyncService

log = structlog.get_logger()
STATUS = {
    "queued": "в очереди",
    "running": "выполняется",
    "succeeded": "успешно завершён",
    "partial": "обработана часть истории; продолжение в следующий ночной анализ",
    "failed": "завершился с ошибкой",
    "interrupted": "прерван; продолжение в следующий ночной анализ",
}
ERRORS = {
    "LLMProviderUnavailable": "Сервис ИИ временно недоступен; автоматический повтор отключён",
    "APITimeoutError": "ИИ не ответил вовремя; сохранённые порции не потеряны",
    "APIConnectionError": "Связь с ИИ прервана; сохранённые порции не потеряны",
    "CancelledError": "Анализ остановлен; сохранённые события остались",
    "AnalysisIdleTimeout": "Анализ слишком долго не сохранял прогресс; текущая порция прервана",
    "TimeoutError": "Истекло время ожидания ответа Telegram или ИИ",
    "AnalysisValidationError": "Ответ ИИ не прошёл проверку формата или источников",
    "InputTooLarge": "Входной контекст ИИ превысил LLM_MAX_INPUT_TOKENS или лимит модели",
    "OutputTooLarge": "Ответ ИИ не поместился в LLM_MAX_OUTPUT_TOKENS",
}


def render_run(run: AnalysisRun, timezone: str = "Europe/Moscow") -> str:
    duration = (
        max(0.0, (utcnow() - run.started_at).total_seconds())
        if run.status == "running"
        else run.duration_seconds
    )
    return (
        f"Анализ: {STATUS.get(run.status, run.status)}.\n"
        f"Начало: {run.started_at.astimezone(ZoneInfo(timezone)):%d.%m.%Y %H:%M}\n"
        + (
            f"Завершение: {run.completed_at.astimezone(ZoneInfo(timezone)):%d.%m.%Y %H:%M}\n"
            if run.completed_at
            else ""
        )
        + f"Чатов подключено: {run.chat_count}\nСообщений собрано: {run.message_count}\n"
        f"Новых событий найдено: {run.event_count}"
        + f"\nНовых/изменённых сообщений обработано: {run.new_message_count}"
        + f"\nНовых конфликтов: {run.conflict_count}; ошибок: {run.error_count}"
        + f"\nДлительность: {duration:.2f} с"
        + (
            "\nДостигнут лимит одного запуска. Текущая порция сохранена. "
            "Продолжение с этого места — в следующий ночной анализ."
            if run.status == "partial"
            else ""
        )
        + (
            f"\nОшибка: {ERRORS.get(run.error, run.error)}. "
            "Необработанные сообщения будут прочитаны повторно."
            if run.error
            else ""
        )
    )


class AnalysisJobs:
    def __init__(self, database: Database, sync: SyncService, notifier: Notifier | None) -> None:
        self.database, self.sync, self.notifier = database, sync, notifier
        self.tasks: dict[UUID, asyncio.Task[None]] = {}
        self.lock = asyncio.Lock()

    async def enqueue(self, user_id: UUID, *, initial: bool = False) -> bool:
        self.sync.require_configured()
        async with self.lock:
            try:
                async with self.database.lock(f"analysis:{user_id}", wait=False):
                    return await self._enqueue_locked(user_id, initial=initial)
            except JobBusy:
                return False

    async def _enqueue_locked(self, user_id: UUID, *, initial: bool) -> bool:
        if user_id in self.tasks or self.sync.locks[user_id].locked():
            return False
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            pending = await session.scalar(
                select(AnalysisRun.id).where(
                    AnalysisRun.user_id == user_id,
                    AnalysisRun.status == "queued",
                )
            )
            if pending:
                return False
            await session.execute(
                update(AnalysisRun)
                .where(
                    AnalysisRun.user_id == user_id,
                    AnalysisRun.status == "running",
                )
                .values(status="interrupted", completed_at=utcnow(), error="ProcessRestart")
            )
            repo = TenantRepository(session, user_id)
            credential = await repo.telegram_session()
            chats = await repo.chats()
            if credential is None or credential.status != "connected":
                raise IntegrationUnavailable("Сначала подключите аккаунт Telegram")
            if not chats:
                raise IntegrationUnavailable("Сначала выберите чаты в «Мои чаты»")
            if initial and not await UserRepository(session).decide_initial_analysis(
                user_id, analyze=True
            ):
                return False
            run = AnalysisRun(
                user_id=user_id,
                model=self.sync.analyzer.model,
                status="queued",
                kind="initial" if initial else "sync",
                chat_count=len(chats),
            )
            session.add(run)
            await session.flush()
        self.tasks[user_id] = asyncio.create_task(self._run(user_id, run.id))
        return True

    async def latest(self, user_id: UUID) -> AnalysisRun | None:
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            return await session.scalar(
                select(AnalysisRun)
                .where(AnalysisRun.user_id == user_id)
                .order_by(AnalysisRun.started_at.desc())
                .limit(1)
            )

    async def stop(self, user_id: UUID) -> bool:
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            queued_id = await session.scalar(
                select(AnalysisRun.id)
                .where(AnalysisRun.user_id == user_id, AnalysisRun.status == "queued")
                .order_by(AnalysisRun.started_at.desc())
                .limit(1)
            )
        task = self.tasks.get(user_id)
        if task is None or task.done():
            return False
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        # Cancellation may happen before the task enters _run's try/finally.
        if self.tasks.get(user_id) is task:
            self.tasks.pop(user_id, None)
        if queued_id:
            await self._finish_queued(queued_id, "interrupted")
        return True

    async def _report_progress(self, user_id: UUID, run_id: UUID) -> None:
        assert self.notifier is not None
        while True:
            await asyncio.sleep(self.sync.settings.analysis_progress_interval_seconds)
            try:
                async with self.database.transaction() as session:
                    user = await UserRepository(session).require(user_id)
                    run = await session.get(AnalysisRun, run_id)
                    if run is None or run.status not in {"running", "queued"}:
                        return
                await self.notifier.send(
                    user.telegram_user_id,
                    render_run(run, user.timezone)
                    + "\nСохранённые дедлайны доступны в главном меню.",
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.warning("Прогресс анализа не доставлен", error_type=type(exc).__name__)
                return

    async def _run(self, user_id: UUID, run_id: UUID) -> None:
        reason = ""
        reporter = (
            asyncio.create_task(self._report_progress(user_id, run_id)) if self.notifier else None
        )
        try:
            async with self.database.transaction() as session:
                user = await UserRepository(session).require(user_id)
                run = await session.get(AnalysisRun, run_id)
                if run is not None and run.kind == "initial" and not user.study_group:
                    raise IntegrationUnavailable("Сначала укажите учебную группу: /group")
            await self.sync.sync_user(user_id, run_id)
        except asyncio.CancelledError:
            await self._finish_queued(run_id, "interrupted")
            raise
        except Exception as exc:
            await self._finish_queued(run_id, "failed")
            reason = (
                str(exc)
                if isinstance(exc, IntegrationUnavailable)
                else "Продолжение — в следующий ночной анализ."
            )
        finally:
            if reporter:
                reporter.cancel()
                await asyncio.gather(reporter, return_exceptions=True)
            self.tasks.pop(user_id, None)
        if self.notifier:
            try:
                async with self.database.transaction() as session:
                    user = await UserRepository(session).require(user_id)
                    run = await session.get(AnalysisRun, run_id)
                    assert run is not None and run.user_id == user_id
                await self.notifier.send(
                    user.telegram_user_id,
                    render_run(run, user.timezone) + (f"\n{reason}" if reason else ""),
                )
                if run.status in {"succeeded", "partial"} or run.new_message_count > 0:
                    await SummaryDelivery(self.database, self.notifier).send_current(user_id)
            except Exception as exc:
                log.warning("Результат анализа не доставлен", error_type=type(exc).__name__)

    async def _finish_queued(self, run_id: UUID, status: str) -> None:
        async with self.database.transaction() as session:
            await session.execute(
                update(AnalysisRun)
                .where(AnalysisRun.id == run_id, AnalysisRun.status == "queued")
                .values(status=status, completed_at=utcnow())
            )

    async def start(self) -> None:
        async with self.database.transaction() as session:
            user_ids = set(
                await session.scalars(
                    select(AnalysisRun.user_id).where(AnalysisRun.status.in_(["running", "queued"]))
                )
            )
        for user_id in user_ids:
            try:
                async with self.database.lock(f"analysis:{user_id}", wait=False):
                    async with self.database.transaction() as session:
                        await session.execute(
                            update(AnalysisRun)
                            .where(
                                AnalysisRun.user_id == user_id,
                                AnalysisRun.status == "running",
                            )
                            .values(
                                status="interrupted", completed_at=utcnow(), error="ProcessRestart"
                            )
                        )
                        queued = list(
                            await session.scalars(
                                select(AnalysisRun)
                                .where(
                                    AnalysisRun.user_id == user_id,
                                    AnalysisRun.status == "queued",
                                )
                                .order_by(AnalysisRun.started_at)
                            )
                        )
                    if queued:
                        self.tasks[user_id] = asyncio.create_task(self._run(user_id, queued[0].id))
            except JobBusy:
                continue  # A live CLI owns this user; its run is not interrupted.

    async def close(self) -> None:
        tasks = list(self.tasks.values())
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
