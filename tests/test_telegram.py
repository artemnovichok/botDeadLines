from collections.abc import AsyncIterator
from unittest.mock import AsyncMock, MagicMock
from uuid import uuid4

from telethon import types

from deadline_assistant.config import Settings
from deadline_assistant.db.models import TrackedChat
from deadline_assistant.domain import utcnow
from deadline_assistant.integrations.crypto import SessionCipher
from deadline_assistant.integrations.telegram import TelegramGateway


async def test_reader_includes_edits_and_service_messages(configured: Settings) -> None:
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    user_id = uuid4()
    chat = TrackedChat(
        user_id=user_id,
        telegram_chat_id=-100123,
        title="Учёба",
        peer_metadata={"kind": "channel", "id": 123, "access_hash": 55},
        last_processed_message_id=10,
    )
    edited = types.Message(
        id=9, peer_id=types.PeerChannel(123), date=utcnow(), edit_date=utcnow(), message="Перенос"
    )
    fresh = types.Message(id=11, peer_id=types.PeerChannel(123), date=utcnow(), message="Задание")
    service = types.MessageService(
        id=12,
        peer_id=types.PeerChannel(123),
        date=utcnow(),
        action=types.MessageActionChatEditTitle(title="Новый заголовок"),
    )
    client = AsyncMock()

    async def iterate(peer: object, **kwargs: object) -> AsyncIterator[object]:
        items = [fresh, service] if kwargs.get("reverse") else [service, fresh, edited]
        for item in items:
            yield item

    client.iter_messages = MagicMock(side_effect=iterate)
    client.is_user_authorized.return_value = True
    gateway.new_client = MagicMock(return_value=client)
    result = await gateway.fetch(cipher.encrypt(user_id, ""), chat)
    assert [message.message_id for message in result] == [9, 11, 12]
    assert result[0].edit_timestamp is not None
    assert result[-1].text == ""
    assert result[1].source_url == "https://t.me/c/123/11"
    client.disconnect.assert_awaited_once()


async def test_initial_history_is_latest_configured_limit(configured):
    configured.initial_message_limit = 3
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    owner = uuid4()
    chat = TrackedChat(
        user_id=owner,
        telegram_chat_id=-1000000000123,
        title="Приватная группа",
        peer_metadata={"kind": "channel", "id": 123, "access_hash": 55},
        last_processed_message_id=0,
        initialization_status="pending",
    )
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_entity.return_value = type("Entity", (), {"username": None})()

    async def iterate(peer, **kwargs):
        assert kwargs["limit"] == 3
        assert not kwargs.get("reverse")
        assert "offset_date" not in kwargs
        for index in (13, 12, 11):
            yield types.Message(
                id=index, peer_id=types.PeerChannel(123), date=utcnow(), message="Доступно"
            )

    client.iter_messages = MagicMock(side_effect=iterate)
    gateway.new_client = MagicMock(return_value=client)
    messages = await gateway.fetch(cipher.encrypt(owner, ""), chat)
    assert [m.message_id for m in messages] == [11, 12, 13]
    assert messages[0].source_url == "https://t.me/c/123/11"


async def test_history_after_reads_forward_and_excludes_old_edit_tail(configured):
    from datetime import timedelta

    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    cutoff = utcnow()
    chat = TrackedChat(
        user_id=uuid4(),
        telegram_chat_id=-123,
        title="Группа",
        peer_metadata={"kind": "chat", "id": 123},
        last_processed_message_id=0,
        history_after=cutoff,
        initialization_status="ready",
    )
    client = AsyncMock()
    client.is_user_authorized.return_value = True

    async def iterate(peer, **kwargs):
        for i, date in [(1, cutoff - timedelta(days=1)), (2, cutoff + timedelta(seconds=1))]:
            yield types.Message(id=i, peer_id=types.PeerChat(123), date=date, message="Текст")

    client.iter_messages = MagicMock(side_effect=iterate)
    gateway.new_client = MagicMock(return_value=client)
    result = await gateway.fetch(cipher.encrypt(chat.user_id, ""), chat)
    assert [m.message_id for m in result] == [2]
    query = client.iter_messages.call_args_list[0].kwargs
    assert query["reverse"] is True and query["offset_date"] == cutoff - timedelta(seconds=1)
    assert query["limit"] == configured.sync_batch_size


def test_message_urls_use_actual_peer_and_no_invented_basic_group_link():
    private = types.Message(id=5, peer_id=types.PeerChannel(456), date=utcnow(), message="Источник")
    assert TelegramGateway._message(private, -1000000000456).source_url == "https://t.me/c/456/5"
    assert (
        TelegramGateway._message(private, -1000000000456, {"username": "public_class"}).source_url
        == "https://t.me/public_class/5"
    )
    basic = types.Message(id=6, peer_id=types.PeerChat(123), date=utcnow(), message="Источник")
    assert TelegramGateway._message(basic, -123).source_url is None


async def test_restore_encrypted_session_and_real_revocation_error(configured):
    import pytest
    from telethon.errors import SessionRevokedError

    from deadline_assistant.domain import SessionExpired

    owner = uuid4()
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    gateway.new_client = MagicMock(return_value=client)
    encrypted = cipher.encrypt(owner, "restored-session")
    async with gateway.connected(owner, encrypted):
        pass
    gateway.new_client.assert_called_once_with("restored-session")
    client.is_user_authorized.side_effect = SessionRevokedError(None)
    with pytest.raises(SessionExpired):
        async with gateway.connected(owner, encrypted):
            raise AssertionError("Revoked session was accepted")
    assert await gateway.revoke(owner, encrypted) is True


async def test_dialogs_include_private_groups_beyond_first_200(configured):
    from types import SimpleNamespace

    owner = uuid4()
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    client = AsyncMock()
    client.is_user_authorized.return_value = True

    async def iterate(**kwargs):
        assert kwargs["limit"] is None
        for i in range(205):
            yield SimpleNamespace(
                id=-1000000000000 - i,
                is_group=True,
                is_channel=True,
                name=f"Группа {i}",
                entity=SimpleNamespace(id=i, access_hash=i + 1, username=None, left=False),
            )

    client.iter_dialogs = MagicMock(side_effect=iterate)
    gateway.new_client = MagicMock(return_value=client)
    dialogs = await gateway.dialogs(owner, cipher.encrypt(owner, ""))
    assert len(dialogs) == 205
    assert dialogs[204]["metadata"]["username"] == ""


async def test_invalidated_duplicate_auth_key_requires_reconnection(configured):
    import pytest
    from telethon.errors import AuthKeyDuplicatedError

    from deadline_assistant.domain import SessionExpired

    owner = uuid4()
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    client = AsyncMock()
    client.connect.side_effect = AuthKeyDuplicatedError(None)
    gateway.new_client = MagicMock(return_value=client)
    with pytest.raises(SessionExpired):
        async with gateway.connected(owner, cipher.encrypt(owner, "invalidated-key")):
            raise AssertionError("Invalidated key accepted")
    client.disconnect.assert_awaited_once()


async def test_direct_source_lookup_batches_ids_and_handles_deleted_messages(configured):
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    owner = uuid4()
    chat = TrackedChat(
        user_id=owner,
        telegram_chat_id=-1000000000123,
        title="Учебная группа",
        peer_metadata={"kind": "channel", "id": 123, "access_hash": 55},
        last_processed_message_id=1,
    )
    client = AsyncMock()
    client.is_user_authorized.return_value = True
    client.get_entity.return_value = type("Entity", (), {"username": None})()

    async def get_messages(peer, ids):
        assert isinstance(peer, types.InputPeerChannel)
        return [
            types.Message(
                id=i,
                peer_id=types.PeerChannel(123),
                date=utcnow(),
                edit_date=utcnow(),
                message="Перенос контрольной",
            )
            if i != 1
            else None
            for i in ids
        ]

    client.get_messages.side_effect = get_messages
    gateway.new_client = MagicMock(return_value=client)
    result = await gateway.fetch_sources(cipher.encrypt(owner, ""), chat, list(range(1, 206)))
    assert result[1] is None
    assert result[2].text == "Перенос контрольной"
    assert result[2].chat_title == "Учебная группа"
    assert result[2].edit_timestamp is not None
    assert [len(c.kwargs["ids"]) for c in client.get_messages.call_args_list] == [100, 100, 5]
    client.iter_messages.assert_not_called()
