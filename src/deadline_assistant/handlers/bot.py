import asyncio
import hashlib
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import TYPE_CHECKING, Any
from uuid import UUID

import structlog
from aiogram import BaseMiddleware, Bot, Dispatcher, F, Router
from aiogram.exceptions import TelegramForbiddenError
from aiogram.filters import Command
from aiogram.fsm.context import FSMContext
from aiogram.fsm.storage.memory import MemoryStorage, SimpleEventIsolation
from aiogram.types import CallbackQuery, ErrorEvent, Message, TelegramObject
from pydantic import ValidationError

from deadline_assistant.db.models import CallbackReceipt, EventList, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import (
    DeliveryBlocked,
    EntityNotFound,
    IntegrationUnavailable,
    StaleState,
    normalize_study_group,
    utcnow,
)
from deadline_assistant.handlers.menus import CONNECT_MENU, MenuStates, create_menu_router
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.digest import ConflictCard, build_digest
from deadline_assistant.services.limits import RateLimiter
from deadline_assistant.services.telegram_text import HtmlText

if TYPE_CHECKING:
    from deadline_assistant.runtime import Runtime

log = structlog.get_logger()
HELP = """Deadline Assistant — учебные дедлайны из ваших чатов.

/connect — подключить Telegram по QR или с одного телефона
/chats — выбор чатов кнопками, поиск и страницы
/track TELEGRAM_CHAT_ID — отслеживать чат из списка
/tracked — отслеживаемые чаты и их ID
/untrack ID — отключить отслеживание
/sync — запустить анализ в фоне
/stop_sync — остановить фоновый анализ; сохранённые события останутся
/events — активные события
/refresh — обновить сводку из БД без нового анализа
/status — последний анализ и его время
/sources ID — источники события
/done ID — завершить событие
/correct ID текст — сразу проанализировать и применить исправление
/undo ID — отменить последнее своё исправление
/conflicts — ожидающие подтверждения изменения
/resolve ID номер — выбрать вариант конфликта (0 — оставить неподтверждённым)
/settings — подключение, пояс, утренняя сводка и последний анализ
/group — указать или изменить учебную группу, например БПИ267
/disconnect — отключить аккаунт и отозвать сессию
/delete_data — полностью удалить личные данные с подтверждением
/help — эта справка

Сообщения выбранных чатов передаются настроенному провайдеру ИИ.
Кнопка «Редактировать дедлайн» применяет изменения сразу.
Отмена многошагового меню: /cancel. Коды и пароли в чат не присылайте."""


async def send_chunks(send: Callable[..., Awaitable[Any]], text: str) -> None:
    for part in telegram_chunks(text):
        await send(part, parse_mode="HTML" if isinstance(text, HtmlText) else None)


def telegram_chunks(text: str) -> list[str]:
    if isinstance(text, HtmlText):
        return text.chunks
    # Telegram's text limit counts UTF-16 units, not Python codepoints (emoji use two).
    parts: list[str] = []
    current: list[str] = []
    units = 0
    for char in text:
        width = 2 if ord(char) > 0xFFFF else 1
        if units + width > 3500:
            parts.append("".join(current))
            current, units = [], 0
        current.append(char)
        units += width
    if current:
        parts.append("".join(current))
    return parts


class BotNotifier:
    def __init__(self, bot: Bot, database: Database | None = None) -> None:
        self.bot = bot
        self.database = database

    async def _send(self, telegram_user_id: int, text: str, **kwargs: Any) -> None:
        try:
            await self.bot.send_message(telegram_user_id, text, **kwargs)
        except TelegramForbiddenError as exc:
            if self.database:
                async with self.database.transaction() as session:
                    user = await UserRepository(session).by_telegram_id(telegram_user_id)
                    if user:
                        user.bot_blocked_at = utcnow()
            raise DeliveryBlocked("Пользователь заблокировал бота") from exc

    async def send(self, telegram_user_id: int, text: str) -> None:
        from deadline_assistant.handlers.event_actions import actions
        from deadline_assistant.handlers.menus import keyboard

        parts = telegram_chunks(text)
        for index, part in enumerate(parts):
            markup = None
            if isinstance(text, HtmlText) and text.list_id and index == len(parts) - 1:
                markup = keyboard(actions(text.list_id))
            await self._send(
                telegram_user_id,
                part,
                parse_mode="HTML" if isinstance(text, HtmlText) else None,
                reply_markup=markup,
            )

    async def send_conflict(self, telegram_user_id: int, card: ConflictCard) -> None:
        from deadline_assistant.handlers.menus import keyboard

        parts = telegram_chunks(card.text)
        for part in parts[:-1]:
            await self._send(telegram_user_id, part, parse_mode=None)
        await self._send(
            telegram_user_id, parts[-1], reply_markup=keyboard([[b] for b in card.buttons])
        )


async def send_digest(message: Message, user: User, runtime: "Runtime") -> None:
    async with runtime.database.transaction() as session:
        await TenantRepository(session, user.id).require_connection()
        cards, summary = await build_digest(
            TenantRepository(session, user.id),
            user.timezone,
            stale_reason="Анализ ИИ недоступен. Показаны сохранённые данные."
            if not runtime.settings.llm_enabled
            else None,
        )
    assert runtime.bot is not None
    notifier = BotNotifier(runtime.bot, runtime.database)
    for card in cards:
        await notifier.send_conflict(user.telegram_user_id, card)
    await notifier.send(user.telegram_user_id, summary)


class UserMiddleware(BaseMiddleware):
    def __init__(self, runtime: "Runtime") -> None:
        self.runtime = runtime
        self.limiter = RateLimiter()

    async def connection_optional(self, event: TelegramObject, data: dict[str, Any]) -> bool:
        if isinstance(event, CallbackQuery):
            value = event.data or ""
            return value.startswith(("auth:", "privacy:", "account:")) or value in {
                "help",
                "menu:cancel",
                "settings:timezone",
                "settings:time",
                "settings:group",
            }
        if not isinstance(event, Message):
            return False
        text = (event.text or "").strip()
        if text.startswith("/"):
            command = text.split(maxsplit=1)[0].split("@", 1)[0]
            return command in {
                "/start",
                "/connect",
                "/help",
                "/settings",
                "/group",
                "/stop_sync",
                "/cancel",
                "/disconnect",
                "/delete_data",
                "/delete_me",
            }
        if text in {"Подключить аккаунт", "Настройки", "Помощь", "Отмена"}:
            return True
        if text in {
            "Мои дедлайны",
            "Мои чаты",
            "Редактировать дедлайн",
            "Завершить задание",
            "Посмотреть источники",
            "Обновить сводку",
            "Отменить своё исправление",
        }:
            return False
        try:
            normalize_study_group(text)
        except ValueError:
            pass
        else:
            return True
        state = data.get("state")
        return state is not None and await state.get_state() in {
            MenuStates.timezone.state,
            MenuStates.digest_time.state,
            MenuStates.study_group.state,
        }

    async def __call__(
        self,
        handler: Callable[[TelegramObject, dict[str, Any]], Awaitable[Any]],
        event: TelegramObject,
        data: dict[str, Any],
    ) -> Any:
        from aiogram.types import User as TelegramUser

        sender: TelegramUser | None
        if isinstance(event, CallbackQuery):
            if not isinstance(event.message, Message):
                return None
            message, sender = event.message, event.from_user
        elif isinstance(event, Message):
            message, sender = event, event.from_user
        else:
            return None
        if sender is None or message.chat.type != "private" or message.chat.id != sender.id:
            return None
        self.limiter.check(f"incoming:{sender.id}", limit=60, seconds=60)
        optional = await self.connection_optional(event, data)
        async with self.runtime.database.transaction() as session:
            user = await UserRepository(session).get_or_create(sender.id, self.runtime.settings)
            # Serializes callback claims across concurrent PostgreSQL transactions as well.
            if user.enabled:
                user = await UserRepository(session).require(user.id)
            user.bot_blocked_at = None
            credential = await TenantRepository(session, user.id).telegram_session()
            needs_connection = not optional and (
                credential is None or credential.status != "connected"
            )
            duplicate = False
            if isinstance(event, CallbackQuery) and not needs_connection:
                # Update redelivery and a second press on one-shot actions are separate cases.
                one_shot = (event.data or "").startswith(
                    (
                        "ep:",
                        "ef:",
                        "cf:",
                        "cfcheck:",
                        "auth:",
                        "analysis:first",
                        "untrack:",
                        "account:confirm_disconnect",
                    )
                ) or (event.data or "").endswith(":save")
                raw = (
                    f"{user.id}:{message.message_id}:{event.data}"
                    if one_shot
                    else f"{user.id}:{event.id}"
                )
                key = hashlib.sha256(raw.encode()).hexdigest()
                duplicate = await session.get(CallbackReceipt, key) is not None
                if not one_shot and not duplicate:
                    tap_key = hashlib.sha256(
                        f"tap:{user.id}:{message.message_id}:{event.data}".encode()
                    ).hexdigest()
                    previous = await session.get(CallbackReceipt, tap_key)
                    if previous and utcnow() - previous.created_at < timedelta(seconds=1):
                        duplicate = True
                    elif previous:
                        previous.created_at = utcnow()
                    else:
                        session.add(CallbackReceipt(key=tap_key, user_id=user.id))
                if not duplicate:
                    session.add(CallbackReceipt(key=key, user_id=user.id))
        if duplicate:
            assert isinstance(event, CallbackQuery)
            await event.answer("Это нажатие уже обработано.")
            return None
        if not user.enabled:
            await message.answer("Ваш профиль отключён.")
            return None
        if needs_connection:
            state = data.get("state")
            if state is not None:
                await state.clear()
            text = "Сначала подключите аккаунт: /connect"
            if isinstance(event, CallbackQuery):
                await event.answer(text, show_alert=True)
            await message.answer(text, reply_markup=CONNECT_MENU)
            return None
        data["user"] = user
        task = asyncio.current_task()
        assert task is not None
        self.runtime.handler_tasks.add(task)
        try:
            return await handler(event, data)
        finally:
            self.runtime.handler_tasks.discard(task)


def create_dispatcher(runtime: "Runtime") -> Dispatcher:
    root = Router()
    root.message.outer_middleware(UserMiddleware(runtime))
    root.callback_query.outer_middleware(UserMiddleware(runtime))
    router = Router()
    router.message.filter(F.chat.type == "private")

    @router.message(Command("delete_data", "delete_me"))
    async def delete_data(message: Message, user: User, state: FSMContext) -> None:
        from deadline_assistant.handlers.menus import keyboard

        await state.clear()
        async with runtime.database.transaction() as session:
            confirmation = EventList(
                user_id=user.id,
                entries=[{"action": "delete_account"}],
                expires_at=utcnow() + timedelta(minutes=10),
            )
            session.add(confirmation)
            await session.flush()
        await message.answer(
            "Удалить аккаунт, Telegram-сессию, выбранные чаты, дедлайны, источники и историю? "
            "Это действие необратимо. Подтверждение действует 10 минут.",
            reply_markup=keyboard(
                [
                    [("Удалить все мои данные", f"privacy:delete:{confirmation.id.hex}")],
                    [("Отмена", "privacy:cancel")],
                ]
            ),
        )

    @router.callback_query(F.data.startswith("privacy:"))
    async def privacy(callback: CallbackQuery, user: User, state: FSMContext) -> None:
        if callback.data == "privacy:cancel":
            await callback.answer("Удаление отменено")
            if isinstance(callback.message, Message):
                await callback.message.edit_reply_markup(reply_markup=None)
            return
        parts = (callback.data or "").split(":")
        if len(parts) != 3 or parts[:2] != ["privacy", "delete"]:
            raise ValueError("Недопустимое подтверждение")
        confirmation_id = UUID(parts[2])
        await callback.answer("Удаляю данные")
        revoked = await runtime.accounts.delete_data(user.id, confirmation_id)
        await state.clear()
        await runtime.login.cleanup()
        if isinstance(callback.message, Message):
            await callback.message.answer(
                "Ваши данные удалены. Для нового подключения используйте /start."
                + (
                    ""
                    if revoked
                    else " Отзыв сессии не подтверждён: завершите её в Telegram → Устройства."
                )
            )

    @router.message(Command("help"))
    @router.message(F.text == "Помощь")
    async def help_command(message: Message) -> None:
        await message.answer(HELP)

    @router.message(Command("track"))
    async def track(message: Message, user: User) -> None:
        parts = (message.text or "").split()
        if len(parts) != 2:
            raise ValueError("Используйте /track TELEGRAM_CHAT_ID из /chats")
        chat_id = await runtime.accounts.track(user.id, int(parts[1]))
        await message.answer(
            f"Чат отслеживается. ID: {chat_id}\nЗапустите /sync для первого сбора."
        )

    @router.message(Command("untrack"))
    async def untrack(message: Message, user: User) -> None:
        async with runtime.database.transaction() as session:
            chat = await TenantRepository(session, user.id).chat(command_uuid(message))
            chat.enabled = False
        await message.answer("Отслеживание отключено.")

    @router.message(Command("events"))
    async def events(message: Message, user: User) -> None:
        await send_digest(message, user, runtime)

    @router.message(Command("done"))
    async def done(message: Message, user: User) -> None:
        async with runtime.database.transaction() as session:
            await TenantRepository(session, user.id).complete(command_uuid(message))
        await message.answer("Событие завершено.")
        await send_digest(message, user, runtime)

    @router.message(Command("sources"))
    async def sources(message: Message, user: User) -> None:
        from deadline_assistant.services.sources import show_sources

        await show_sources(message, user, runtime, command_uuid(message))

    @router.message(Command("correct"))
    async def correct(message: Message, user: User) -> None:
        parts = (message.text or "").split(maxsplit=2)
        if len(parts) != 3:
            raise ValueError("Используйте /correct ID текст исправления")
        status = await submit_correction(
            runtime, user.id, UUID(parts[1]), parts[2], message.chat.id, message.message_id
        )
        await message.answer(
            "Дедлайн обновлён."
            if status == "applied"
            else "Исправление сохранено и ожидает обработки или подтверждения."
        )
        await send_digest(message, user, runtime)

    @router.message(Command("conflicts"))
    async def conflicts(message: Message, user: User) -> None:
        async with runtime.database.transaction() as session:
            cards, _ = await build_digest(TenantRepository(session, user.id), user.timezone)
        assert runtime.bot is not None
        for card in cards:
            await BotNotifier(runtime.bot).send_conflict(user.telegram_user_id, card)
        if not cards:
            await message.answer("Нерешённых конфликтов нет.")

    @router.message(Command("resolve"))
    async def resolve(message: Message, user: User) -> None:
        parts = (message.text or "").split()
        if len(parts) != 3 or not parts[2].isdigit():
            raise ValueError("Используйте /resolve ID_КОНФЛИКТА номер_варианта")
        async with runtime.database.transaction() as session:
            await TenantRepository(session, user.id).resolve_conflict(UUID(parts[1]), int(parts[2]))
        await send_digest(message, user, runtime)

    @root.errors()
    async def error(event: ErrorEvent) -> bool:
        exception = event.exception
        if isinstance(exception, EntityNotFound):
            text = "Объект не найден. Проверьте ID в списке ваших объектов."
        elif isinstance(exception, IntegrationUnavailable):
            text = str(exception)
        elif isinstance(exception, StaleState):
            text = str(exception) or "Событие изменилось. Откройте новый список."
        elif isinstance(exception, (TelegramForbiddenError, DeliveryBlocked)):
            update = event.update
            telegram_id = (
                update.message.chat.id
                if update.message
                else update.callback_query.from_user.id
                if update.callback_query
                else None
            )
            if telegram_id:
                async with runtime.database.transaction() as session:
                    blocked = await UserRepository(session).by_telegram_id(telegram_id)
                    if blocked:
                        blocked.bot_blocked_at = utcnow()
            return True
        elif isinstance(exception, (ValidationError, ValueError)):
            text = "Проверьте формат команды и значения. Справка: /help."
        elif isinstance(exception, TimeoutError):
            text = "Превышено время ожидания. Повторите попытку позже."
        else:
            text = "Не удалось выполнить команду. Повторите попытку позже."
        log.warning("Ошибка команды", error_type=type(exception).__name__)
        if event.update.message and event.update.message.chat.type == "private":
            await event.update.message.answer(text)
        elif event.update.callback_query:
            callback = event.update.callback_query
            if isinstance(callback.message, Message) and callback.message.chat.type == "private":
                await callback.answer(text[:180], show_alert=True)
        return True

    from deadline_assistant.handlers.event_actions import create_event_router

    root.include_router(create_event_router(runtime))
    root.include_router(router)
    root.include_router(create_menu_router(runtime))
    dispatcher = Dispatcher(storage=MemoryStorage(), events_isolation=SimpleEventIsolation())
    dispatcher.include_router(root)
    return dispatcher


def command_uuid(message: Message) -> UUID:
    parts = (message.text or "").split()
    if len(parts) != 2:
        raise ValueError("Нужно указать ID объекта")
    return UUID(parts[1])


async def submit_correction(
    runtime: "Runtime",
    user_id: UUID,
    event_id: UUID,
    text: str,
    chat_id: int,
    message_id: int,
    *,
    expected_version: int | None = None,
) -> str:
    try:
        return await runtime.corrections.submit(
            user_id,
            event_id,
            text,
            chat_id,
            message_id,
            expected_version=expected_version,
        )
    except (ValueError, EntityNotFound):
        raise
    except Exception as exc:
        from sqlalchemy import select

        from deadline_assistant.db.models import UserCorrection

        async with runtime.database.transaction() as session:
            saved = await session.scalar(
                select(UserCorrection.id).where(
                    UserCorrection.user_id == user_id,
                    UserCorrection.telegram_chat_id == chat_id,
                    UserCorrection.telegram_message_id == message_id,
                )
            )
        if saved is None:
            raise
        log.error(
            "Исправление ожидает повторного анализа",
            user_id=str(user_id),
            error_type=type(exc).__name__,
        )
        return "pending"
