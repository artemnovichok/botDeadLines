import asyncio
from collections.abc import Callable
from contextlib import AsyncExitStack
from typing import Any

import structlog
from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from sqlalchemy import select

from deadline_assistant.config import Settings
from deadline_assistant.db.models import AuthLink, User
from deadline_assistant.db.schema import check_schema
from deadline_assistant.db.session import Database
from deadline_assistant.handlers.bot import BotNotifier, create_dispatcher
from deadline_assistant.handlers.menus import MENU, keyboard
from deadline_assistant.integrations.crypto import SessionCipher
from deadline_assistant.integrations.llm import OpenAIAnalyzer
from deadline_assistant.integrations.telegram import TelegramGateway
from deadline_assistant.services.accounts import AccountService
from deadline_assistant.services.auth import AuthService, QRLoginManager
from deadline_assistant.services.corrections import CorrectionService
from deadline_assistant.services.cycle import CycleService
from deadline_assistant.services.jobs import AnalysisJobs
from deadline_assistant.services.requests import BotRequestMiddleware
from deadline_assistant.services.retention import RetentionService
from deadline_assistant.services.scheduler import SchedulerService
from deadline_assistant.services.sync import SyncService

log = structlog.get_logger()


class Runtime:
    def __init__(self, settings: Settings, *, readonly: bool = False) -> None:
        self.settings = settings
        self.lifetime = AsyncExitStack()
        self.stopping = False
        self.polling_failed = False
        self.on_polling_failure: Callable[[], None] | None = None
        self.database = Database(settings, readonly=readonly)
        cipher = (
            SessionCipher(settings.session_encryption_key.get_secret_value())
            if settings.session_encryption_key
            else None
        )
        self.gateway = TelegramGateway(settings, cipher, self.database)
        self.analyzer = OpenAIAnalyzer(settings, self.database)
        self.auth = AuthService(self.database)
        self.login = QRLoginManager(self.database, self.gateway)
        self.accounts = AccountService(self.database, self.gateway)
        self.sync = SyncService(self.database, self.gateway, self.analyzer, settings)
        self.corrections = CorrectionService(self.database, self.analyzer, settings)
        self.retention = RetentionService(self.database, settings)
        self.bot = (
            Bot(
                settings.bot_token.get_secret_value(),
                default=DefaultBotProperties(link_preview_is_disabled=True),
            )
            if settings.bot_token
            else None
        )
        notifier = BotNotifier(self.bot, self.database) if self.bot else None
        if self.bot:
            self.bot.session.middleware(BotRequestMiddleware(self.database, settings))
        self.cycle = CycleService(self.database, settings, self.sync, notifier)
        self.accounts.notifier = notifier
        self.sync.accounts = self.accounts
        self.jobs = AnalysisJobs(self.database, self.sync, notifier)
        self.auth.on_exchange = self._confirm_browser if self.bot else None
        self.login.on_connected = self._connected if self.bot else None
        self.login.on_revoke_failed = self._revocation_unconfirmed if self.bot else None
        self.dispatcher: Dispatcher | None = create_dispatcher(self) if self.bot else None
        self.bot_task: asyncio.Task[None] | None = None
        self.handler_tasks: set[asyncio.Task[Any]] = set()
        self.scheduler = SchedulerService(
            self.database,
            settings,
            self.sync,
            notifier,
            self.retention,
        )

    async def start(self) -> None:
        try:
            await self.lifetime.enter_async_context(self.database.lock("server", wait=False))
            await check_schema(self.database)
            async with self.database.transaction() as session:
                await session.execute(select(User.id).limit(1))
            await self.scheduler.start()
            await self.jobs.start()
            self.scheduler.scheduler.add_job(
                self.scheduler.maintenance,
                "interval",
                args=[self.login.cleanup],
                seconds=30,
                id="auth_cleanup",
                max_instances=1,
                coalesce=True,
            )
            if self.bot and self.dispatcher:
                self.bot_task = asyncio.create_task(
                    self.dispatcher.start_polling(
                        self.bot,
                        handle_signals=False,
                        close_bot_session=False,
                        tasks_concurrency_limit=20,
                    )
                )
                self.bot_task.add_done_callback(self._polling_done)
            log.info(
                "Приложение запущено",
                bot_enabled=bool(self.bot),
                telegram_enabled=self.settings.telegram_enabled,
                llm_enabled=self.settings.llm_enabled,
            )
        except BaseException as exc:
            log.error(
                "Ошибка запуска: проверьте настройки и миграции", error_type=type(exc).__name__
            )
            await self.analyzer.close()
            if self.bot:
                await self.bot.session.close()
            await self.lifetime.aclose()
            await self.database.close()
            raise

    def _polling_done(self, task: asyncio.Task[None]) -> None:
        if self.stopping:
            return
        self.polling_failed = True
        exception = task.exception() if not task.cancelled() else None
        log.error("Опрос Telegram остановлен", error_type=type(exception).__name__)
        if self.on_polling_failure:
            self.on_polling_failure()

    async def _confirm_browser(self, link: AuthLink) -> None:
        from deadline_assistant.repositories.users import UserRepository

        assert self.bot is not None
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(link.user_id)
        await self.bot.send_message(
            user.telegram_user_id,
            f"Открыта страница подключения, заявка {link.id.hex[:6].upper()}. "
            "Сравните номер со страницей и подтвердите только если открыли её сами. "
            "Если вы её не открывали, нажмите «Отменить».",
            reply_markup=keyboard(
                [
                    [
                        ("Это я", f"auth:approve:{link.id.hex}"),
                        ("Отменить", f"auth:cancel:{link.id.hex}"),
                    ]
                ]
            ),
        )

    async def _connected(self, user: User) -> None:
        assert self.bot is not None
        await self.bot.send_message(
            user.telegram_user_id,
            "Аккаунт успешно подключён. Нажмите «Мои чаты» и выберите источники.",
            reply_markup=MENU,
        )

    async def _revocation_unconfirmed(self, user: User) -> None:
        assert self.bot is not None
        await self.bot.send_message(
            user.telegram_user_id,
            "Новый аккаунт подключён, но отзыв предыдущей сессии не подтверждён. "
            "Завершите старую сессию Deadline Assistant в Telegram → Настройки → Устройства.",
        )

    async def stop(self) -> None:
        self.stopping = True
        self.scheduler.stopping = True
        self.scheduler.scheduler.pause()
        if self.bot_task:
            if self.dispatcher and not self.bot_task.done():
                try:
                    await self.dispatcher.stop_polling()
                except RuntimeError:
                    self.bot_task.cancel()
            await asyncio.gather(self.bot_task, return_exceptions=True)
        if self.handler_tasks:
            done, pending = await asyncio.wait(
                self.handler_tasks, timeout=self.settings.sync_timeout_seconds + 5
            )
            for task in pending:
                task.cancel()
            await asyncio.gather(*done, *pending, return_exceptions=True)
        await self.scheduler.stop()
        await self.jobs.close()
        await self.login.close()
        await self.analyzer.close()
        if self.bot:
            await self.bot.session.close()
        await self.lifetime.aclose()
        await self.database.close()
        log.info("Приложение остановлено")
