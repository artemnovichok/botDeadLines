import asyncio
from collections import defaultdict
from collections.abc import Callable
from datetime import timedelta
from functools import partial
from time import monotonic
from uuid import UUID

import structlog
from sqlalchemy import func, select, update

from deadline_assistant.config import Settings
from deadline_assistant.db.models import AnalysisRun, Conflict, TrackedChat
from deadline_assistant.db.session import Database
from deadline_assistant.domain import (
    AnalysisContext,
    AnalysisIdleTimeout,
    AnalysisOutput,
    AnalysisResult,
    AnalysisSliceComplete,
    InputTooLarge,
    IntegrationUnavailable,
    MessageData,
    SessionExpired,
    utcnow,
)
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.accounts import AccountService
from deadline_assistant.services.analysis import MemoryBuilder, OperationApplier
from deadline_assistant.services.batches import analyze_batch
from deadline_assistant.services.interfaces import Analyzer, MessageReader
from deadline_assistant.services.requests import retry

log = structlog.get_logger()


class SyncService:
    def __init__(
        self,
        database: Database,
        reader: MessageReader,
        analyzer: Analyzer,
        settings: Settings,
        accounts: AccountService | None = None,
    ) -> None:
        self.database, self.reader, self.analyzer, self.settings = (
            database,
            reader,
            analyzer,
            settings,
        )
        self.accounts = accounts
        self.locks: defaultdict[UUID, asyncio.Lock] = defaultdict(asyncio.Lock)

    def require_configured(self) -> None:
        if not self.settings.llm_enabled or not self.settings.telegram_enabled:
            raise IntegrationUnavailable("Сбор требует настройки Telegram и ИИ")

    @staticmethod
    async def _check(repo: TenantRepository, encrypted: str, chat_id: UUID) -> TrackedChat:
        await UserRepository(repo.session).require(repo.user_id)
        credential = await repo.telegram_session()
        if (
            credential is None
            or credential.status != "connected"
            or credential.encrypted_session_data != encrypted
        ):
            raise IntegrationUnavailable("Подключение аккаунта изменено или отозвано")
        stored = await repo.chat(chat_id)
        if not stored.enabled:
            raise IntegrationUnavailable("Отслеживание чата отключено")
        return stored

    async def sync_user(self, user_id: UUID, run_id: UUID | None = None) -> int:
        self.require_configured()
        async with self.locks[user_id]:
            async with self.database.lock(
                f"analysis:{user_id}", timeout=self.settings.job_lock_timeout_seconds
            ):
                return await self._sync_user(user_id, run_id)

    async def _sync_user(self, user_id: UUID, run_id: UUID | None) -> int:
        started = monotonic()
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            await session.execute(
                update(AnalysisRun)
                .where(
                    AnalysisRun.user_id == user_id,
                    AnalysisRun.status == "running",
                )
                .values(status="interrupted", completed_at=utcnow(), error="ProcessRestart")
            )
            if run_id is None:
                run = AnalysisRun(user_id=user_id, model=self.analyzer.model, status="running")
                session.add(run)
                await session.flush()
            else:
                existing = await session.get(AnalysisRun, run_id)
                if existing is None or existing.user_id != user_id or existing.status != "queued":
                    raise IntegrationUnavailable("Задание уже запущено или отменено")
                run = existing
                run.status = "running"
            run_id = run.id
        encrypted: str | None = None
        watchdog: asyncio.Timeout | None = None
        try:
            async with asyncio.timeout(self.settings.sync_timeout_seconds) as watchdog:

                def progress() -> None:
                    watchdog.reschedule(
                        asyncio.get_running_loop().time() + self.settings.sync_timeout_seconds
                    )
                    if monotonic() - started >= self.settings.sync_max_duration_seconds:
                        raise AnalysisSliceComplete

                async with self.database.transaction() as session:
                    repo = TenantRepository(session, user_id)
                    credential = await repo.telegram_session()
                    if credential is None or credential.status != "connected":
                        raise IntegrationUnavailable("Сначала подключите аккаунт Telegram")
                    encrypted = credential.encrypted_session_data
                    chats = await repo.chats()
                    saved_run = await session.get(AnalysisRun, run_id)
                    assert saved_run is not None
                    saved_run.chat_count = len(chats)
                seen: set[tuple[int, int]] = set()
                for chat in chats:
                    await retry(
                        partial(
                            self._refresh_sources,
                            user_id,
                            encrypted,
                            chat,
                            run_id,
                            on_progress=progress,
                        ),
                        self.settings.retry_attempts,
                    )
                    progress()
                    while True:
                        async with self.database.transaction() as session:
                            await UserRepository(session).require(user_id)
                            chat = await self._check(
                                TenantRepository(session, user_id), encrypted, chat.id
                            )
                        starting = chat.last_processed_message_id == 0
                        messages = sorted(
                            await retry(
                                partial(self.reader.fetch, encrypted, chat),
                                self.settings.retry_attempts,
                            ),
                            key=lambda m: m.message_id,
                        )
                        # A cursor is an ID boundary, never an object to look up.
                        for index, message in enumerate(messages):
                            if not message.chat_title:
                                messages[index] = message.model_copy(
                                    update={"chat_title": chat.title}
                                )
                        if any(m.chat_id != chat.telegram_chat_id for m in messages):
                            raise ValueError("Сборщик вернул сообщения из другого чата")
                        seen.update((m.chat_id, m.message_id) for m in messages)
                        new_count = sum(
                            m.message_id > chat.last_processed_message_id for m in messages
                        )
                        async with self.database.transaction() as session:
                            repo = TenantRepository(session, user_id)
                            stored = await self._check(repo, encrypted, chat.id)
                            if starting and stored.initial_upper_message_id is None and messages:
                                stored.initial_upper_message_id = max(
                                    m.message_id for m in messages
                                )
                            saved_run = await session.get(AnalysisRun, run_id)
                            assert saved_run is not None
                            saved_run.message_count = len(seen)
                        # Первоначальная история ограничена; LLM получает короткие порции.
                        for offset in range(0, len(messages), self.settings.sync_batch_size):
                            await self._batch(
                                user_id,
                                encrypted,
                                chat.id,
                                run_id,
                                messages[offset : offset + self.settings.sync_batch_size],
                                on_progress=progress,
                            )
                        if starting or new_count < self.settings.sync_batch_size:
                            async with self.database.transaction() as session:
                                stored = await self._check(
                                    TenantRepository(session, user_id), encrypted, chat.id
                                )
                                stored.last_successful_sync_at = utcnow()
                                stored.initialization_status = "ready"
                            progress()
                            break
                from deadline_assistant.services.corrections import CorrectionService

                await CorrectionService(self.database, self.analyzer, self.settings).resume_pending(
                    user_id, on_progress=progress
                )
                async with self.database.transaction() as session:
                    saved_run = await session.get(AnalysisRun, run_id)
                    assert saved_run is not None
                    saved_run.status, saved_run.completed_at = "succeeded", utcnow()
                    saved_run.duration_seconds = monotonic() - started
                    log.info(
                        "Анализ завершён",
                        user_id=str(user_id),
                        duration_seconds=saved_run.duration_seconds,
                        message_count=saved_run.message_count,
                        new_message_count=saved_run.new_message_count,
                        event_count=saved_run.event_count,
                        conflict_count=saved_run.conflict_count,
                        usage=saved_run.token_usage,
                        last_successful_analysis=saved_run.completed_at.isoformat(),
                    )
                    return saved_run.event_count
        except AnalysisSliceComplete:
            async with self.database.transaction() as session:
                saved_run = await session.get(AnalysisRun, run_id)
                assert saved_run is not None
                saved_run.status, saved_run.completed_at = "partial", utcnow()
                saved_run.duration_seconds = monotonic() - started
                return saved_run.event_count
        except BaseException as exc:
            failure = (
                AnalysisIdleTimeout("Анализ остановлен: слишком долго нет сохранённого прогресса")
                if isinstance(exc, TimeoutError) and watchdog is not None and watchdog.expired()
                else exc
            )
            async with self.database.transaction() as session:
                saved_run = await session.get(AnalysisRun, run_id)
                if saved_run is None:
                    raise  # The owner deleted their account during the external request.
                saved_run.status = (
                    "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
                )
                saved_run.completed_at, saved_run.error = utcnow(), type(failure).__name__
                saved_run.duration_seconds = monotonic() - started
                saved_run.error_count += 1
            if isinstance(exc, SessionExpired) and self.accounts and encrypted:
                await self.accounts.mark_revoked(user_id, encrypted)
            log.warning("Ошибка анализа", user_id=str(user_id), error_type=type(failure).__name__)
            if failure is not exc:
                raise failure from None
            raise

    async def _batch(
        self,
        user_id: UUID,
        encrypted: str,
        chat_id: UUID,
        run_id: UUID,
        messages: list[MessageData],
        *,
        refresh_sources: bool = False,
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(user_id)
            repo = TenantRepository(session, user_id)
            await self._check(repo, encrypted, chat_id)
            pending = [m for m in messages if not await repo.is_processed(m)]
            if not pending:
                return
            saved_run = await session.get(AnalysisRun, run_id)
            assert saved_run is not None
            timezone = user.timezone
            now = utcnow()
            cutoff = now - timedelta(days=self.settings.message_max_age_days)
            recent = [
                m
                for m in pending
                if refresh_sources or max(m.timestamp, m.edit_timestamp or m.timestamp) >= cutoff
            ]
            consumed = max(
                saved_run.token_usage.get("total_tokens", 0),
                saved_run.token_usage.get("prompt_tokens", 0)
                + saved_run.token_usage.get("completion_tokens", 0),
            )
            if recent and consumed >= self.settings.sync_max_total_tokens:
                raise AnalysisSliceComplete
            context = (
                await MemoryBuilder(self.settings.llm_memory_event_limit).build(
                    repo, recent, timezone, now=now
                )
                if recent
                else AnalysisContext(
                    timezone=timezone, current_time=now, study_group=user.study_group
                )
            )
        try:
            if recent:
                async with self.database.lock(f"chat:{user_id}:{chat_id}", timeout=600):
                    output = await analyze_batch(
                        self.database, self.analyzer, user_id, recent, timezone, context
                    )
            else:
                output = AnalysisOutput(result=AnalysisResult())
        except InputTooLarge:
            if len(messages) < 2:
                raise
            middle = len(messages) // 2
            await self._batch(
                user_id,
                encrypted,
                chat_id,
                run_id,
                messages[:middle],
                refresh_sources=refresh_sources,
                on_progress=on_progress,
            )
            await self._batch(
                user_id,
                encrypted,
                chat_id,
                run_id,
                messages[middle:],
                refresh_sources=refresh_sources,
                on_progress=on_progress,
            )
            return
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            repo = TenantRepository(session, user_id)
            stored = await self._check(repo, encrypted, chat_id)
            # Another analysis may already have committed this exact batch.
            if all([await repo.is_processed(m) for m in pending]):
                return
            conflict_query = (
                select(func.count()).select_from(Conflict).where(Conflict.user_id == user_id)
            )
            conflicts_before = await session.scalar(conflict_query) or 0
            count = await OperationApplier().apply(repo, output.result, recent, context)
            await session.flush()
            conflicts_after = await session.scalar(conflict_query) or 0
            for message in pending:
                await repo.mark_processed(message)
            stored.last_processed_message_id = max(
                stored.last_processed_message_id, max(m.message_id for m in messages)
            )
            stored.last_successful_sync_at = utcnow()
            saved_run = await session.get(AnalysisRun, run_id)
            assert saved_run is not None
            saved_run.event_count += count
            saved_run.new_message_count += len(pending)
            saved_run.conflict_count += max(0, conflicts_after - conflicts_before)
            usage = dict(saved_run.token_usage)
            for key, value in output.token_usage.items():
                if isinstance(value, (int, float)):
                    usage[key] = usage.get(key, 0) + value
            saved_run.token_usage = usage
        # Renew only after commit; cancellation/failure never acknowledges an uncommitted batch.
        if on_progress:
            on_progress()

    async def _refresh_sources(
        self,
        user_id: UUID,
        encrypted: str,
        chat: TrackedChat,
        run_id: UUID,
        *,
        on_progress: Callable[[], None] | None = None,
    ) -> None:
        from sqlalchemy import select

        from deadline_assistant.db.models import (
            Conflict,
            ConflictOption,
            ConflictOptionSource,
            Event,
            EventSource,
            SourceMessage,
        )

        async with self.database.transaction() as session:
            repo = TenantRepository(session, user_id)
            await self._check(repo, encrypted, chat.id)
            event_sources = (
                select(EventSource.source_id)
                .join(Event, Event.id == EventSource.event_id)
                .where(Event.user_id == user_id, Event.status == "active")
            )
            conflict_sources = (
                select(ConflictOptionSource.source_id)
                .join(ConflictOption, ConflictOption.id == ConflictOptionSource.option_id)
                .join(Conflict, Conflict.id == ConflictOption.conflict_id)
                .where(Conflict.user_id == user_id, Conflict.status.in_(["open", "unconfirmed"]))
            )
            sources = list(
                await session.scalars(
                    select(SourceMessage).where(
                        SourceMessage.user_id == user_id,
                        SourceMessage.source_chat_id == chat.telegram_chat_id,
                        SourceMessage.id.in_(event_sources)
                        | SourceMessage.id.in_(conflict_sources),
                    )
                )
            )
        if not sources:
            return
        current = await self.reader.fetch_sources(
            encrypted, chat, sorted({s.source_message_id for s in sources})
        )
        pending = []
        async with self.database.transaction() as session:
            repo = TenantRepository(session, user_id)
            await self._check(repo, encrypted, chat.id)
            for message_id, message in current.items():
                if message_id not in {s.source_message_id for s in sources}:
                    raise ValueError("Неожиданный ID при проверке источников")
                if message is not None and (
                    message.chat_id != chat.telegram_chat_id or message.message_id != message_id
                ):
                    raise ValueError("Подмена источника сборщиком")
                versions = [s for s in sources if s.source_message_id == message_id]
                for detached in versions:
                    stored = await session.get(SourceMessage, detached.id)
                    assert stored is not None
                    stored.available = message is not None
                    stored.checked_at = utcnow()
                if message is not None:
                    latest = max(versions, key=lambda s: s.edit_timestamp or s.message_timestamp)
                    if (
                        message.text != latest.original_text
                        or message.edit_timestamp != latest.edit_timestamp
                    ):
                        pending.append(message)
        # Each edit is analysed before new history; its old ID cannot regress the cursor.
        for offset in range(0, len(pending), self.settings.sync_batch_size):
            await self._batch(
                user_id,
                encrypted,
                chat.id,
                run_id,
                sorted(
                    pending[offset : offset + self.settings.sync_batch_size],
                    key=lambda m: m.message_id,
                ),
                refresh_sources=True,
                on_progress=on_progress,
            )
