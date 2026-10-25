import asyncio
from datetime import timedelta
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from cryptography.fernet import InvalidToken
from sqlalchemy import select
from telethon.errors import SessionPasswordNeededError

from deadline_assistant.config import Settings
from deadline_assistant.db.models import AuthLink, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import EntityNotFound, utcnow
from deadline_assistant.integrations.crypto import SessionCipher
from deadline_assistant.integrations.telegram import TelegramGateway
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.auth import AuthService, QRLoginManager


async def test_auth_link_one_time_and_csrf(database: Database, users: tuple[User, User]) -> None:
    service = AuthService(database)
    token = await service.issue(users[0].id)
    browser, csrf = await service.exchange(token)
    assert (await service.authenticate(browser, csrf)).user_id == users[0].id
    for operation in [
        service.exchange(token),
        service.authenticate(browser, "wrong"),
        service.authenticate("wrong"),
    ]:
        with pytest.raises(EntityNotFound):
            await operation
    async with database.transaction() as session:
        saved = await session.scalar(select(AuthLink))
        assert token not in saved.token_hash
        assert browser not in saved.browser_hash
    await service.issue(users[0].id)
    with pytest.raises(EntityNotFound):
        await service.authenticate(browser, csrf)


async def test_concurrent_exchange_only_one_succeeds(
    database: Database, users: tuple[User, User]
) -> None:
    service = AuthService(database)
    token = await service.issue(users[0].id)
    result = await asyncio.gather(
        service.exchange(token), service.exchange(token), return_exceptions=True
    )
    assert sum(isinstance(value, tuple) for value in result) == 1
    assert sum(isinstance(value, EntityNotFound) for value in result) == 1


async def test_expired_link_and_disabled_user(database: Database, users: tuple[User, User]) -> None:
    service = AuthService(database)
    token = await service.issue(users[0].id)
    async with database.transaction() as session:
        link = await session.scalar(select(AuthLink))
        link.expires_at = utcnow() - timedelta(seconds=1)
    with pytest.raises(EntityNotFound):
        await service.exchange(token)
    token = await service.issue(users[0].id)
    browser, csrf = await service.exchange(token)
    async with database.transaction() as session:
        user = await session.get(User, users[0].id)
        user.enabled = False
    with pytest.raises(EntityNotFound):
        await service.authenticate(browser, csrf)


def test_cipher_is_bound_to_user(settings: Settings) -> None:
    from uuid import uuid4

    cipher = SessionCipher(settings.session_encryption_key.get_secret_value())
    owner = uuid4()
    encrypted = cipher.encrypt(owner, "private-session-string")
    assert "private-session-string" not in encrypted
    assert cipher.decrypt(owner, encrypted) == "private-session-string"
    with pytest.raises(InvalidToken):
        cipher.decrypt(uuid4(), encrypted)
    with pytest.raises(InvalidToken):
        cipher.decrypt(owner, encrypted[:-5] + "wrong")


@pytest.mark.parametrize("correct_owner", [True, False])
async def test_qr_identity_check_and_encrypted_storage(
    database: Database, users: tuple[User, User], configured: Settings, correct_owner: bool
) -> None:
    auth = AuthService(database)
    browser, _ = await auth.exchange(await auth.issue(users[0].id))
    link = await auth.authenticate(browser)
    await auth.approve(users[0].id, link.id)
    link = await auth.authenticate(browser)
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    client = AsyncMock()
    client.session = SimpleNamespace(save=lambda: "test-private-session")
    client.get_me.return_value = SimpleNamespace(
        id=users[0 if correct_owner else 1].telegram_user_id
    )
    qr = SimpleNamespace(wait=AsyncMock(), url="tg://login?token=test")
    client.qr_login.return_value = qr
    gateway.new_client = MagicMock(return_value=client)
    manager = QRLoginManager(database, gateway)
    try:
        await manager.begin(link)
        gateway.new_client.assert_called_once_with(receive_updates=True)
        await manager.pending[link.id].task
        async with database.transaction() as session:
            stored = await TenantRepository(session, users[0].id).telegram_session()
        if correct_owner:
            assert manager.state(link) == "connected"
            assert (
                cipher.decrypt(users[0].id, stored.encrypted_session_data) == "test-private-session"
            )
        else:
            assert manager.state(link) == "wrong_account"
            assert stored is None
            client.log_out.assert_awaited_once()
    finally:
        await manager.close()


async def test_qr_two_factor_flow(
    database: Database, users: tuple[User, User], configured: Settings
) -> None:
    auth = AuthService(database)
    browser, _ = await auth.exchange(await auth.issue(users[0].id))
    link = await auth.authenticate(browser)
    await auth.approve(users[0].id, link.id)
    link = await auth.authenticate(browser)
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    client = AsyncMock()
    client.session = SimpleNamespace(save=lambda: "session")
    client.get_me.return_value = SimpleNamespace(id=users[0].telegram_user_id)
    client.qr_login.return_value = SimpleNamespace(
        url="tg://login?token=test",
        wait=AsyncMock(side_effect=SessionPasswordNeededError(request=None)),
    )
    gateway.new_client = MagicMock(return_value=client)
    manager = QRLoginManager(database, gateway)
    try:
        await manager.begin(link)
        await manager.pending[link.id].task
        assert manager.state(link) == "password_required"
        assert await manager.password(link, "2fa-password") == "connected"
        client.sign_in.assert_awaited_once_with(password="2fa-password")
    finally:
        await manager.close()
