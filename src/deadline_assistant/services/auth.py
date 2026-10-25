import asyncio
import hashlib
import secrets
from collections.abc import Awaitable, Callable
from contextlib import suppress
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from uuid import UUID

import structlog
from sqlalchemy import delete, select, update
from telethon import types
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    PhoneNumberInvalidError,
    RPCError,
    SessionPasswordNeededError,
)

from deadline_assistant.db.models import AuthLink, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import (
    AuthFlowError,
    EntityNotFound,
    IntegrationUnavailable,
    RateLimited,
    utcnow,
)
from deadline_assistant.integrations.telegram import TelegramGateway
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.limits import RateLimiter

log = structlog.get_logger()


def digest_secret(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


class AuthService:
    def __init__(self, database: Database) -> None:
        self.database = database
        self.limiter = RateLimiter()
        self.on_exchange: Callable[[AuthLink], Awaitable[None]] | None = None

    async def issue(self, user_id: UUID) -> str:
        self.limiter.check(str(user_id), limit=5, seconds=600)
        token = secrets.token_urlsafe(32)
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            await session.execute(delete(AuthLink).where(AuthLink.user_id == user_id))
            session.add(
                AuthLink(
                    user_id=user_id,
                    token_hash=digest_secret(token),
                    expires_at=utcnow() + timedelta(minutes=10),
                )
            )
        return token

    async def exchange(self, token: str) -> tuple[str, str]:
        browser, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        async with self.database.transaction() as session:
            enabled = select(User.id).where(User.enabled.is_(True))
            result = await session.execute(
                update(AuthLink)
                .where(
                    AuthLink.token_hash == digest_secret(token),
                    AuthLink.consumed_at.is_(None),
                    AuthLink.expires_at > utcnow(),
                    AuthLink.user_id.in_(enabled),
                )
                .values(
                    browser_hash=digest_secret(browser),
                    csrf_hash=digest_secret(csrf),
                    consumed_at=utcnow(),
                )
                .returning(AuthLink.id)
            )
            link_id = result.scalar_one_or_none()
            if link_id is None:
                raise EntityNotFound
            link = await session.get(AuthLink, link_id)
            assert link is not None
        if self.on_exchange:
            try:
                await self.on_exchange(link)
            except Exception:
                await self.cancel(link.user_id, link.id)
                raise IntegrationUnavailable("Не удалось отправить подтверждение в бот") from None
        return browser, csrf

    async def authenticate(self, browser: str, csrf: str | None = None) -> AuthLink:
        async with self.database.transaction() as session:
            link = await session.scalar(
                select(AuthLink)
                .join(User, User.id == AuthLink.user_id)
                .where(
                    AuthLink.browser_hash == digest_secret(browser),
                    AuthLink.expires_at > utcnow(),
                    User.enabled.is_(True),
                )
            )
            if link is None:
                raise EntityNotFound
            if csrf is not None and not secrets.compare_digest(
                link.csrf_hash or "", digest_secret(csrf)
            ):
                raise EntityNotFound
            return link

    async def approve(self, user_id: UUID, link_id: UUID) -> None:
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            result = await session.execute(
                update(AuthLink)
                .where(
                    AuthLink.id == link_id,
                    AuthLink.user_id == user_id,
                    AuthLink.consumed_at.is_not(None),
                    AuthLink.approved_at.is_(None),
                    AuthLink.completed_at.is_(None),
                    AuthLink.expires_at > utcnow(),
                )
                .values(approved_at=utcnow())
                .returning(AuthLink.id)
            )
            if result.scalar_one_or_none() is None:
                raise EntityNotFound

    async def cancel(self, user_id: UUID, link_id: UUID) -> None:
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            result = await session.execute(
                delete(AuthLink)
                .where(AuthLink.id == link_id, AuthLink.user_id == user_id)
                .returning(AuthLink.id)
            )
            if result.scalar_one_or_none() is None:
                raise EntityNotFound


@dataclass
class PendingLogin:
    client: Any = field(repr=False)
    user_id: UUID
    qr: Any = field(default=None, repr=False)
    state: str = "waiting"
    task: asyncio.Task[None] | None = field(default=None, repr=False)
    phone: str | None = field(default=None, repr=False)
    phone_code_hash: str | None = field(default=None, repr=False)
    delivery: str = ""
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)


class QRLoginManager:
    """QR и телефон используют один короткоживущий, подтверждённый ботом браузерный вход."""

    def __init__(self, database: Database, gateway: TelegramGateway) -> None:
        self.database, self.gateway = database, gateway
        self.pending: dict[UUID, PendingLogin] = {}
        self.lock = asyncio.Lock()
        self.finalizers: set[asyncio.Task[None]] = set()
        self.on_connected: Callable[[User], Awaitable[None]] | None = None
        self.on_revoke_failed: Callable[[User], Awaitable[None]] | None = None

    async def _validate(self, link: AuthLink, *, attempt: bool = False) -> AuthLink:
        exhausted = False
        async with self.database.transaction() as session:
            await UserRepository(session).require(link.user_id)
            current = await session.get(AuthLink, link.id)
            if (
                current is None
                or current.user_id != link.user_id
                or current.expires_at <= utcnow()
                or current.consumed_at is None
            ):
                raise EntityNotFound
            if current.approved_at is None:
                raise AuthFlowError("Подтвердите открытие страницы в личном чате с ботом")
            if attempt:
                if current.completed_at is not None:
                    raise AuthFlowError("Этот вход уже завершён")
                if current.attempt_count >= 5:
                    exhausted = True
                else:
                    current.attempt_count += 1
        if exhausted:
            raise AuthFlowError("Лимит попыток исчерпан. Получите новую ссылку в боте")
        return current

    async def begin(self, link: AuthLink) -> str:
        async with self.lock:
            current = await self._validate(link)
            if current.completed_at is not None:
                return "connected"
            existing = self.pending.get(link.id)
            if existing:
                return existing.state
            pending = await self._new(link)
            try:
                async with asyncio.timeout(30):
                    pending.qr = await pending.client.qr_login()
                pending.task = asyncio.create_task(self._wait(link.id, pending))
                await asyncio.sleep(0)
                return pending.state
            except BaseException as exc:
                await self._abort(pending)
                self._raise_api_error(exc)
                raise

    async def _new(self, link: AuthLink) -> PendingLogin:
        if len(self.pending) >= 100:
            raise IntegrationUnavailable("Слишком много подключений. Повторите позже")
        client = self.gateway.new_client(receive_updates=True)
        pending = PendingLogin(client=client, user_id=link.user_id)
        self.pending[link.id] = pending
        try:
            async with asyncio.timeout(30):
                await client.connect()
        except BaseException as exc:
            await self._abort(pending)
            self._raise_api_error(exc)
            raise
        return pending

    async def phone(self, link: AuthLink, phone: str) -> str:
        async with self.lock:
            await self._validate(link)
            if link.id in self.pending:
                raise AuthFlowError(
                    "Вход уже начат. Для смены способа отмените его и получите новую ссылку"
                )
            await self._validate(link, attempt=True)
            pending = await self._new(link)
            pending.phone = phone
            try:
                async with asyncio.timeout(30):
                    sent = await pending.client.send_code_request(phone)
                    if isinstance(sent, types.auth.SentCodeSuccess):
                        await self._finish(link.id, pending)
                    elif isinstance(sent, types.auth.SentCode):
                        # Варианты, требующие email setup, оплаты или Firebase, не эмулируем.
                        supported = {
                            types.auth.SentCodeTypeApp: "Код отправлен в приложение Telegram.",
                            types.auth.SentCodeTypeSms: "Telegram отправил код по SMS.",
                            types.auth.SentCodeTypeCall: "Telegram сообщит код голосовым звонком.",
                            types.auth.SentCodeTypeEmailCode: "Код отправлен на вашу почту входа.",
                        }
                        pending.delivery = supported.get(type(sent.type), "")
                        if not pending.delivery or not sent.phone_code_hash:
                            raise AuthFlowError(
                                "Этот способ подтверждения недоступен этому клиенту. "
                                "Используйте QR со второго устройства или повторите вход позже"
                            )
                        pending.phone_code_hash = sent.phone_code_hash
                        pending.state = "code_required"
                    else:
                        raise AuthFlowError(
                            "Telegram не разрешил вход по коду. Используйте QR-вход"
                        )
            except BaseException as exc:
                await self._abort(pending)
                self._raise_api_error(exc)
                raise
            if pending.state == "connected":
                await pending.client.disconnect()
            return pending.state

    async def code(self, link: AuthLink, code: str) -> str:
        pending = self.pending.get(link.id)
        if pending is None:
            raise AuthFlowError("Вход прерван. Получите новую ссылку в боте")
        async with pending.lock:
            if pending.state != "code_required":
                raise AuthFlowError("Сначала запросите код на этой странице")
            current = await self._validate(link, attempt=True)
            try:
                async with asyncio.timeout(30):
                    await pending.client.sign_in(
                        phone=pending.phone, code=code, phone_code_hash=pending.phone_code_hash
                    )
                    await self._finish(link.id, pending)
            except SessionPasswordNeededError:
                pending.phone_code_hash = None
                pending.state = "password_required"
            except PhoneCodeInvalidError:
                if current.attempt_count >= 5:
                    await self._abort(pending)
                raise AuthFlowError("Неверный код входа") from None
            except BaseException as exc:
                await self._abort(pending)
                self._raise_api_error(exc)
                raise
            if pending.state not in {"code_required", "password_required"}:
                await pending.client.disconnect()
            return pending.state

    async def _wait(self, link_id: UUID, pending: PendingLogin) -> None:
        try:
            await pending.qr.wait()
            async with pending.lock, asyncio.timeout(30):
                await self._finish(link_id, pending)
        except SessionPasswordNeededError:
            pending.state = "password_required"
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            pending.state = "expired" if isinstance(exc, TimeoutError) else "failed"
            log.warning("QR-вход не завершён", error_type=type(exc).__name__)
        finally:
            if pending.state != "password_required":
                await pending.client.disconnect()

    async def password(self, link: AuthLink, password: str) -> str:
        pending = self.pending.get(link.id)
        if pending is None:
            raise AuthFlowError("Вход прерван. Получите новую ссылку в боте")
        async with pending.lock:
            if pending.state != "password_required":
                raise AuthFlowError("Сначала подтвердите вход средствами Telegram")
            current = await self._validate(link, attempt=True)
            try:
                async with asyncio.timeout(30):
                    await pending.client.sign_in(password=password)
                    await self._finish(link.id, pending)
            except PasswordHashInvalidError:
                if current.attempt_count >= 5:
                    await self._abort(pending)
                raise AuthFlowError("Неверный пароль двухэтапной аутентификации") from None
            except BaseException as exc:
                await self._abort(pending)
                self._raise_api_error(exc)
                raise
            if pending.state != "password_required":
                await pending.client.disconnect()
            return pending.state

    @staticmethod
    def _raise_api_error(exc: BaseException) -> None:
        if isinstance(exc, FloodWaitError):
            raise RateLimited(exc.seconds) from None
        if isinstance(exc, PhoneNumberInvalidError):
            raise AuthFlowError("Проверьте номер телефона в международном формате") from None
        if isinstance(exc, PhoneCodeExpiredError):
            raise AuthFlowError("Код истёк. Получите новую ссылку в боте") from None
        if isinstance(exc, (RPCError, OSError, TimeoutError)):
            raise IntegrationUnavailable(
                "Telegram не завершил вход. Повторите позже с новой ссылкой; "
                "если доступен QR-вход, используйте его"
            ) from None

    async def _finish(self, link_id: UUID, pending: PendingLogin) -> None:
        revoke = True
        previous: str | None = None
        try:
            async with asyncio.timeout(20):
                me = await pending.client.get_me()
            async with self.database.transaction() as session:
                user = await UserRepository(session).require(pending.user_id)
                link = await session.get(AuthLink, link_id)
                if (
                    link is None
                    or link.expires_at <= utcnow()
                    or link.completed_at is not None
                    or link.approved_at is None
                ):
                    raise EntityNotFound
                if link.user_id != user.id:
                    raise EntityNotFound
                if (
                    me is None
                    or me.id != user.telegram_user_id
                    or getattr(me, "bot", False) is True
                ):
                    pending.state = "wrong_account"
                    return
                cipher = self.gateway.cipher
                assert cipher is not None
                encrypted = cipher.encrypt(user.id, pending.client.session.save())
                repo = TenantRepository(session, user.id)
                old = await repo.telegram_session()
                previous = old.encrypted_session_data if old else None
                await repo.save_session(encrypted)
                link.completed_at = utcnow()
            revoke = False
            pending.phone = pending.phone_code_hash = None
            pending.state = "connected"
            task = asyncio.create_task(self._after_connected(user, previous, encrypted))
            self.finalizers.add(task)
            task.add_done_callback(self.finalizers.discard)
        finally:
            if revoke:
                with suppress(Exception):
                    async with asyncio.timeout(5):
                        await pending.client.log_out()

    async def _after_connected(self, user: User, previous: str | None, encrypted: str) -> None:
        revoked = not previous or await self.gateway.revoke(user.id, previous)
        if not revoked:
            log.warning("Старую Telegram-сессию нужно завершить в разделе Устройства")
        async with self.database.transaction() as session:
            credential = await TenantRepository(session, user.id).telegram_session()
            if (
                credential is None
                or credential.status != "connected"
                or credential.encrypted_session_data != encrypted
            ):
                return
        if self.on_connected:
            try:
                async with asyncio.timeout(15):
                    await self.on_connected(user)
            except Exception as exc:
                log.warning(
                    "Уведомление о подключении не доставлено", error_type=type(exc).__name__
                )
        if not revoked and self.on_revoke_failed:
            try:
                async with asyncio.timeout(15):
                    await self.on_revoke_failed(user)
            except Exception as exc:
                log.warning("Уведомление об отзыве не доставлено", error_type=type(exc).__name__)

    def state(self, link: AuthLink) -> str:
        if link.completed_at is not None:
            return "connected"
        if link.approved_at is None:
            return "approval_required"
        pending = self.pending.get(link.id)
        return pending.state if pending else "idle"

    def qr_url(self, link_id: UUID) -> str:
        pending = self.pending.get(link_id)
        if pending is None or pending.state != "waiting" or pending.qr is None:
            raise EntityNotFound
        return str(pending.qr.url)

    async def cleanup(self) -> None:
        async with self.database.transaction() as session:
            alive = set(
                await session.scalars(select(AuthLink.id).where(AuthLink.expires_at > utcnow()))
            )
            await session.execute(delete(AuthLink).where(AuthLink.expires_at <= utcnow()))
        for link_id in list(self.pending):
            if link_id not in alive:
                await self._remove(link_id)

    async def _abort(self, pending: PendingLogin) -> None:
        pending.state = "failed"
        pending.phone = pending.phone_code_hash = None
        with suppress(Exception):
            async with asyncio.timeout(5):
                await pending.client.log_out()
        await pending.client.disconnect()

    async def _remove(self, link_id: UUID) -> None:
        pending = self.pending.pop(link_id, None)
        if pending:
            if pending.task and not pending.task.done():
                pending.task.cancel()
                await asyncio.gather(pending.task, return_exceptions=True)
            async with pending.lock:
                if pending.state != "connected":
                    await self._abort(pending)
                else:
                    await pending.client.disconnect()

    async def cancel(self, link: AuthLink) -> None:
        await AuthService(self.database).cancel(link.user_id, link.id)
        await self._remove(link.id)

    async def close(self) -> None:
        for link_id in list(self.pending):
            await self._remove(link_id)
        if self.finalizers:
            await asyncio.gather(*self.finalizers, return_exceptions=True)
