import asyncio
import re
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Any, TypedDict
from uuid import UUID

from cryptography.fernet import InvalidToken
from telethon import TelegramClient, types, utils
from telethon.errors import (
    AuthKeyDuplicatedError,
    ChannelInvalidError,
    ChannelPrivateError,
    ChatAdminRequiredError,
    FloodWaitError,
    RPCError,
    UnauthorizedError,
)
from telethon.sessions import StringSession

from deadline_assistant.config import Settings
from deadline_assistant.db.models import TrackedChat
from deadline_assistant.db.session import Database
from deadline_assistant.domain import (
    IntegrationUnavailable,
    MessageData,
    RateLimited,
    SessionExpired,
    TransientIntegrationError,
)
from deadline_assistant.integrations.crypto import SessionCipher
from deadline_assistant.services.requests import throttle


class ThrottledTelegramClient(TelegramClient):  # type: ignore[misc]
    budget_database: Database | None = None
    budget_interval: float = 0.15

    async def __call__(
        self,
        request: Any,
        ordered: bool = False,
        flood_sleep_threshold: int | None = None,
    ) -> Any:
        if self.budget_database:
            await throttle(self.budget_database, "telegram-mtproto", self.budget_interval)
        return await super().__call__(request, ordered, flood_sleep_threshold)


class TelegramDialog(TypedDict):
    id: int
    title: str
    metadata: dict[str, int | str]


class TelegramGateway:
    def __init__(
        self,
        settings: Settings,
        cipher: SessionCipher | None,
        database: Database | None = None,
    ) -> None:
        self.settings = settings
        self.cipher = cipher
        self.database = database

    def new_client(self, session: str = "", *, receive_updates: bool = False) -> Any:
        if not self.settings.telegram_enabled:
            raise IntegrationUnavailable("Подключение Telegram не настроено")
        assert self.settings.telegram_api_hash is not None
        client = ThrottledTelegramClient(
            StringSession(session),
            self.settings.telegram_api_id,
            self.settings.telegram_api_hash.get_secret_value(),
            connection_retries=3,
            request_retries=3,
            raise_last_call_error=True,
            flood_sleep_threshold=0,
            receive_updates=receive_updates,
            device_model="Deadline Assistant",
        )
        client.budget_database = self.database
        client.budget_interval = self.settings.telegram_request_interval
        return client

    @asynccontextmanager
    async def connected(self, user_id: UUID, encrypted: str) -> AsyncIterator[Any]:
        if self.database:
            async with self.database.lock(f"telegram-session:{user_id}", timeout=30):
                async with self._connected(user_id, encrypted) as client:
                    yield client
        else:
            async with self._connected(user_id, encrypted) as client:
                yield client

    @asynccontextmanager
    async def _connected(self, user_id: UUID, encrypted: str) -> AsyncIterator[Any]:
        if self.cipher is None:
            raise IntegrationUnavailable("Шифрование сессий не настроено")
        try:
            client = self.new_client(self.cipher.decrypt(user_id, encrypted))
        except (InvalidToken, ValueError):
            raise SessionExpired("Сессия недоступна; подключите Telegram заново") from None
        try:
            await client.connect()
            if not await client.is_user_authorized():
                raise SessionExpired("Сессия Telegram отозвана; подключите аккаунт заново")
            yield client
        except (UnauthorizedError, AuthKeyDuplicatedError):
            raise SessionExpired(
                "Авторизация Telegram истекла; подключите аккаунт заново"
            ) from None
        except FloodWaitError as exc:
            raise RateLimited(exc.seconds) from None
        except (ChannelPrivateError, ChannelInvalidError, ChatAdminRequiredError):
            raise IntegrationUnavailable(
                "Telegram не предоставил доступ к чату. Обновите список и проверьте членство в нём"
            ) from None
        except (OSError, TimeoutError, RPCError):
            raise TransientIntegrationError(
                "Telegram временно недоступен. Повторите позже"
            ) from None
        finally:
            await client.disconnect()

    async def revoke(self, user_id: UUID, encrypted: str) -> bool:
        try:
            async with asyncio.timeout(20), self.connected(user_id, encrypted) as client:
                return bool(await client.log_out())
        except SessionExpired:
            return True
        except Exception:
            return False

    async def dialogs(self, user_id: UUID, encrypted: str) -> list[TelegramDialog]:
        async with self.connected(user_id, encrypted) as client:
            result: list[TelegramDialog] = []
            async for dialog in client.iter_dialogs(limit=None):
                if not (dialog.is_group or dialog.is_channel):
                    continue
                entity = dialog.entity
                if getattr(entity, "left", False) or isinstance(
                    entity, (types.ChannelForbidden, types.ChatForbidden)
                ):
                    continue
                result.append(
                    {
                        "id": dialog.id,
                        "title": dialog.name or "Без названия",
                        "metadata": {
                            "kind": "channel" if dialog.is_channel else "chat",
                            "id": entity.id,
                            "access_hash": getattr(entity, "access_hash", None) or 0,
                            "username": getattr(entity, "username", None) or "",
                        },
                    }
                )
            return result

    async def fetch(self, encrypted_session: str, chat: TrackedChat) -> list[MessageData]:
        async with self.connected(chat.user_id, encrypted_session) as client:
            metadata = chat.peer_metadata
            if metadata.get("kind") == "channel":
                peer = types.InputPeerChannel(int(metadata["id"]), int(metadata["access_hash"]))
            else:
                peer = types.InputPeerChat(int(metadata["id"]))
            # Данные из текущей сущности корректируют устаревший/изменившийся username.
            entity = await client.get_entity(peer)
            current_metadata = {
                **metadata,
                "title": chat.title,
                "username": getattr(entity, "username", None) or "",
            }
            messages: dict[int, MessageData] = {}
            if chat.last_processed_message_id == 0:
                async for item in client.iter_messages(
                    peer,
                    limit=self.settings.initial_message_limit,
                    max_id=(chat.initial_upper_message_id + 1)
                    if chat.initial_upper_message_id
                    else 0,
                ):
                    if isinstance(item, (types.Message, types.MessageService)):
                        messages[item.id] = self._message(
                            item, chat.telegram_chat_id, current_metadata
                        )
            else:
                async for item in client.iter_messages(
                    peer,
                    min_id=chat.last_processed_message_id,
                    reverse=True,
                    limit=self.settings.sync_batch_size,
                    max_id=(chat.initial_upper_message_id + 1)
                    if chat.initialization_status == "pending" and chat.initial_upper_message_id
                    else 0,
                ):
                    if isinstance(item, (types.Message, types.MessageService)):
                        messages[item.id] = self._message(
                            item, chat.telegram_chat_id, current_metadata
                        )
                async for item in client.iter_messages(
                    peer, limit=self.settings.edit_lookback_limit
                ):
                    if isinstance(item, (types.Message, types.MessageService)) and (
                        item.id <= chat.last_processed_message_id
                        and chat.initialization_status != "pending"
                    ):
                        messages[item.id] = self._message(
                            item, chat.telegram_chat_id, current_metadata
                        )
            return sorted(messages.values(), key=lambda message: message.message_id)

    async def fetch_sources(
        self, encrypted_session: str, chat: TrackedChat, message_ids: list[int]
    ) -> dict[int, MessageData | None]:
        """Direct ID lookup works even if the sync cursor itself was deleted."""
        if not message_ids:
            return {}
        async with self.connected(chat.user_id, encrypted_session) as client:
            metadata = chat.peer_metadata
            peer = (
                types.InputPeerChannel(int(metadata["id"]), int(metadata["access_hash"]))
                if metadata.get("kind") == "channel"
                else types.InputPeerChat(int(metadata["id"]))
            )
            entity = await client.get_entity(peer)
            metadata = {
                **metadata,
                "title": chat.title,
                "username": getattr(entity, "username", None) or "",
            }
            result: dict[int, MessageData | None] = {}
            ids = sorted(set(message_ids))
            for offset in range(0, len(ids), 100):
                batch = ids[offset : offset + 100]
                items = await client.get_messages(peer, ids=batch)
                found = {item.id: item for item in items if isinstance(item, types.Message)}
                for message_id in batch:
                    item = found.get(message_id)
                    result[message_id] = (
                        self._message(item, chat.telegram_chat_id, metadata) if item else None
                    )
            return result

    @staticmethod
    def _message(item: Any, chat_id: int, metadata: dict[str, Any] | None = None) -> MessageData:
        url = None
        metadata = metadata or {}
        username = metadata.get("username", "")
        peer = getattr(item, "peer_id", None)
        if isinstance(peer, types.PeerChannel):
            if isinstance(username, str) and re.fullmatch(r"[A-Za-z][A-Za-z0-9_]{3,31}", username):
                url = f"https://t.me/{username}/{item.id}"
            else:
                url = f"https://t.me/c/{peer.channel_id}/{item.id}"
        sender = item.sender_id
        return MessageData(
            chat_id=chat_id,
            chat_title=str(metadata.get("title", ""))[:300],
            message_id=item.id,
            text=getattr(item, "message", None) or "",
            timestamp=item.date,
            edit_timestamp=getattr(item, "edit_date", None),
            author_id=utils.get_peer_id(sender) if sender else None,
            source_url=url,
            author_name=(utils.get_display_name(item.sender)[:300] if item.sender else None),
            reply_to_message_id=getattr(getattr(item, "reply_to", None), "reply_to_msg_id", None),
        )
