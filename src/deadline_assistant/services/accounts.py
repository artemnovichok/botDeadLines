import asyncio
from uuid import UUID

from sqlalchemy import delete, select

from deadline_assistant.db.models import (
    AuthLink,
    EventList,
    RequestBudget,
    TelegramSession,
    TrackedChat,
)
from deadline_assistant.db.session import Database
from deadline_assistant.domain import EntityNotFound, IntegrationUnavailable, SessionExpired, utcnow
from deadline_assistant.integrations.telegram import TelegramDialog, TelegramGateway
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.interfaces import Notifier


class AccountService:
    def __init__(
        self, database: Database, gateway: TelegramGateway, notifier: Notifier | None = None
    ) -> None:
        self.database, self.gateway, self.notifier = database, gateway, notifier

    async def credential(self, user_id: UUID) -> str:
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            credential = await TenantRepository(session, user_id).telegram_session()
            if credential is None or credential.status != "connected":
                raise IntegrationUnavailable("Сначала подключите аккаунт: /connect")
            return credential.encrypted_session_data

    async def mark_revoked(self, user_id: UUID, encrypted: str) -> None:
        notify = False
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(user_id)
            credential = await TenantRepository(session, user_id).telegram_session()
            if credential and credential.encrypted_session_data == encrypted:
                notify = credential.status == "connected"
                credential.status, credential.last_error = "revoked", "SessionExpired"
        if notify and self.notifier:
            try:
                await self.notifier.send(
                    user.telegram_user_id,
                    "Сессия Telegram отозвана или истекла. Подключите аккаунт заново: /connect. "
                    "Ваши дедлайны сохранены.",
                )
            except Exception:
                pass  # Статус виден в настройках, даже если бот заблокирован.

    async def dialogs(self, user_id: UUID) -> list[TelegramDialog]:
        encrypted = await self.credential(user_id)
        try:
            async with asyncio.timeout(120):
                result = await self.gateway.dialogs(user_id, encrypted)
        except SessionExpired:
            await self.mark_revoked(user_id, encrypted)
            raise
        if await self.credential(user_id) != encrypted:
            raise IntegrationUnavailable("Подключение изменилось. Обновите список чатов")
        return result

    async def select(self, user_id: UUID, selected: set[int]) -> int:
        encrypted = await self.credential(user_id)
        available = {d["id"]: d for d in await self.dialogs(user_id)}
        if selected - available.keys():
            raise IntegrationUnavailable(
                "Часть чатов недоступна. Обновите список и повторите выбор"
            )
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(user_id)
            repo = TenantRepository(session, user_id)
            credential = await repo.telegram_session()
            if (
                credential is None
                or credential.status != "connected"
                or credential.encrypted_session_data != encrypted
            ):
                raise IntegrationUnavailable("Подключение изменилось. Откройте «Мои чаты» заново")
            tracked = await repo.chats()
            previously_selected = {chat.telegram_chat_id for chat in tracked}
            for chat in tracked:
                if chat.telegram_chat_id not in selected:
                    chat.enabled = False
            for chat_id in selected:
                item = available[chat_id]
                chat = await repo.track(chat_id, item["title"], item["metadata"])
                if user.initial_analysis_choice is not None and chat_id not in previously_selected:
                    chat.history_after = utcnow().replace(microsecond=0)
                    chat.initialization_status = "ready"
                    chat.last_processed_message_id = 0
                    chat.initial_upper_message_id = None
        return len(selected)

    async def track(self, user_id: UUID, telegram_chat_id: int) -> UUID:
        encrypted = await self.credential(user_id)
        available = {d["id"]: d for d in await self.dialogs(user_id)}
        if telegram_chat_id not in available:
            raise EntityNotFound
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(user_id)
            repo = TenantRepository(session, user_id)
            credential = await repo.telegram_session()
            if (
                credential is None
                or credential.status != "connected"
                or credential.encrypted_session_data != encrypted
            ):
                raise IntegrationUnavailable("Подключение аккаунта было отменено")
            selected = available[telegram_chat_id]
            previously_selected = {c.telegram_chat_id for c in await repo.chats()}
            chat = await repo.track(telegram_chat_id, selected["title"], selected["metadata"])
            if (
                user.initial_analysis_choice is not None
                and telegram_chat_id not in previously_selected
            ):
                chat.history_after = utcnow().replace(microsecond=0)
                chat.initialization_status = "ready"
                chat.last_processed_message_id = 0
                chat.initial_upper_message_id = None
            return chat.id

    async def untrack(self, user_id: UUID, chat_id: UUID) -> None:
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            (await TenantRepository(session, user_id).chat(chat_id)).enabled = False

    async def connections(self, user_id: UUID) -> list[TrackedChat]:
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            return list(
                await session.scalars(select(TrackedChat).where(TrackedChat.user_id == user_id))
            )

    async def disconnect(self, user_id: UUID) -> bool:
        # Удаление локальных прав немедленно прерывает входы и запрещает commit анализа.
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            repo = TenantRepository(session, user_id)
            credential = await repo.telegram_session()
            await session.execute(delete(AuthLink).where(AuthLink.user_id == user_id))
            await session.execute(delete(TelegramSession).where(TelegramSession.user_id == user_id))
            for chat in await repo.chats():
                chat.enabled = False
        if credential:
            return await self.gateway.revoke(user_id, credential.encrypted_session_data)
        return True

    async def delete_data(self, user_id: UUID, confirmation_id: UUID) -> bool:
        from deadline_assistant.domain import utcnow

        # Delete permissions and data first, even if Telegram is temporarily unreachable.
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(user_id)
            confirmation = await session.get(EventList, confirmation_id)
            if (
                confirmation is None
                or confirmation.user_id != user_id
                or confirmation.expires_at <= utcnow()
                or confirmation.entries != [{"action": "delete_account"}]
            ):
                raise EntityNotFound("Подтверждение истекло; повторите /delete_data")
            credential = await TenantRepository(session, user_id).telegram_session()
            await session.execute(
                delete(RequestBudget).where(
                    RequestBudget.key == f"bot-chat:{user.telegram_user_id}"
                )
            )
            await session.delete(user)
        if credential:
            return await self.gateway.revoke(user_id, credential.encrypted_session_data)
        return True
