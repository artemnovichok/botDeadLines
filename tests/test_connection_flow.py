import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from sqlalchemy import select
from telethon import types
from telethon.errors import (
    FloodWaitError,
    PasswordHashInvalidError,
    PhoneCodeExpiredError,
    PhoneCodeInvalidError,
    SessionPasswordNeededError,
)

from deadline_assistant.config import Settings
from deadline_assistant.db.models import AuthLink, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import EntityNotFound, IntegrationUnavailable, RateLimited
from deadline_assistant.integrations.crypto import SessionCipher
from deadline_assistant.integrations.telegram import TelegramGateway
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.auth import AuthService, QRLoginManager


async def login_context(database: Database, user: User, configured: Settings, *, approve=True):
    auth = AuthService(database)
    token = await auth.issue(user.id)
    browser, csrf = await auth.exchange(token)
    link = await auth.authenticate(browser, csrf)
    if approve:
        await auth.approve(user.id, link.id)
        link = await auth.authenticate(browser, csrf)
    cipher = SessionCipher(configured.session_encryption_key.get_secret_value())
    gateway = TelegramGateway(configured, cipher)
    client = AsyncMock()
    client.session = SimpleNamespace(save=lambda: "private-test-session")
    client.get_me.return_value = SimpleNamespace(id=user.telegram_user_id, bot=False)
    client.send_code_request.return_value = types.auth.SentCode(
        type=types.auth.SentCodeTypeApp(length=5),
        phone_code_hash="private-code-hash",
    )
    gateway.new_client = MagicMock(return_value=client)
    manager = QRLoginManager(database, gateway)
    return auth, browser, csrf, link, gateway, client, manager


async def test_stolen_link_requires_owner_bot_approval(database, users, configured):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database,
        users[0],
        configured,
        approve=False,
    )
    with pytest.raises(EntityNotFound):
        await auth.approve(users[1].id, link.id)
    with pytest.raises(EntityNotFound):
        await auth.cancel(users[1].id, link.id)
    with pytest.raises(ValueError, match="Подтвердите"):
        await manager.begin(link)
    with pytest.raises(ValueError, match="Подтвердите"):
        await manager.phone(link, "+79991234567")
    gateway.new_client.assert_not_called()
    await auth.approve(users[0].id, link.id)
    with pytest.raises(EntityNotFound):
        await auth.approve(users[0].id, link.id)
    assert (await auth.authenticate(browser, csrf)).approved_at is not None
    await manager.close()


@pytest.mark.parametrize("two_factor", [False, True])
async def test_single_phone_login_and_notification(database, users, configured, two_factor):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    manager.on_connected = AsyncMock()
    if two_factor:
        client.sign_in.side_effect = [SessionPasswordNeededError(None), SimpleNamespace(id=101)]
    try:
        assert await manager.phone(link, "+79991234567") == "code_required"
        assert "Telegram" in manager.pending[link.id].delivery
        result = await manager.code(link, "12345")
        client.sign_in.assert_any_await(
            phone="+79991234567", code="12345", phone_code_hash="private-code-hash"
        )
        if two_factor:
            assert result == "password_required"
            assert await manager.password(link, "private-2fa") == "connected"
            client.sign_in.assert_any_await(password="private-2fa")
        else:
            assert result == "connected"
        await asyncio.gather(*manager.finalizers)
        manager.on_connected.assert_awaited_once()
        async with database.transaction() as session:
            saved = await TenantRepository(session, users[0].id).telegram_session()
            assert (
                gateway.cipher.decrypt(users[0].id, saved.encrypted_session_data)
                == "private-test-session"
            )
            assert await TenantRepository(session, users[1].id).telegram_session() is None
        assert (await auth.authenticate(browser, csrf)).completed_at is not None
    finally:
        await manager.close()


@pytest.mark.parametrize("password_mode", [False, True])
async def test_attempt_limit_and_no_secret_in_errors(database, users, configured, password_mode):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    try:
        await manager.phone(link, "+79991234567")
        if password_mode:
            manager.pending[link.id].state = "password_required"
            client.sign_in.side_effect = PasswordHashInvalidError(None)

            async def operation():
                return await manager.password(link, "private-password")
        else:
            client.sign_in.side_effect = PhoneCodeInvalidError(None)

            async def operation():
                return await manager.code(link, "12345")

        for _ in range(4):
            with pytest.raises(ValueError) as error:
                await operation()
            assert "private-password" not in str(error.value)
            assert "12345" not in str(error.value)
        assert manager.state(link) == "failed"
        calls = client.sign_in.await_count
        with pytest.raises(ValueError):
            await operation()
        assert client.sign_in.await_count == calls
        async with database.transaction() as session:
            stored = await session.get(AuthLink, link.id)
            assert stored.attempt_count == 5
    finally:
        await manager.close()


async def test_cancel_waiting_qr_and_replay(database, users, configured):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    forever = asyncio.Event()
    client.qr_login.return_value = SimpleNamespace(url="tg://login?token=test", wait=forever.wait)
    await manager.begin(link)
    task = manager.pending[link.id].task
    await manager.cancel(link)
    assert task.done()
    assert not manager.pending
    with pytest.raises(EntityNotFound):
        await auth.authenticate(browser, csrf)
    with pytest.raises(EntityNotFound):
        await manager.begin(link)
    client.log_out.assert_awaited_once()
    async with database.transaction() as session:
        assert await TenantRepository(session, users[0].id).telegram_session() is None


async def test_cancellation_racing_sign_in_does_not_save_session(database, users, configured):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    await manager.phone(link, "+79991234567")
    entered, finish = asyncio.Event(), asyncio.Event()

    async def sign_in(**kwargs):
        entered.set()
        await finish.wait()

    client.sign_in.side_effect = sign_in
    task = asyncio.create_task(manager.code(link, "12345"))
    await entered.wait()
    await auth.cancel(users[0].id, link.id)
    finish.set()
    with pytest.raises(EntityNotFound):
        await task
    async with database.transaction() as session:
        assert await TenantRepository(session, users[0].id).telegram_session() is None
    await manager.close()


@pytest.mark.parametrize(
    "error,expected",
    [
        (FloodWaitError(None, capture=45), RateLimited),
        (PhoneCodeExpiredError(None), ValueError),
        (OSError("sensitive-upstream-detail"), IntegrationUnavailable),
    ],
)
async def test_telegram_login_errors_fail_closed(database, users, configured, error, expected):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    try:
        await manager.phone(link, "+79991234567")
        client.sign_in.side_effect = error
        with pytest.raises(expected) as caught:
            await manager.code(link, "12345")
        assert "sensitive-upstream-detail" not in str(caught.value)
        assert manager.state(link) == "failed"
        async with database.transaction() as session:
            assert await TenantRepository(session, users[0].id).telegram_session() is None
    finally:
        await manager.close()


async def test_reconnect_revokes_previous_session(database, users, configured):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    gateway.revoke = AsyncMock(return_value=True)
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("old-encrypted")
    await manager.phone(link, "+79991234567")
    assert await manager.code(link, "12345") == "connected"
    await asyncio.gather(*manager.finalizers)
    gateway.revoke.assert_awaited_once_with(users[0].id, "old-encrypted")
    await manager.close()


async def test_restarted_login_does_not_restore_secrets(database, users, configured):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    await manager.phone(link, "+79991234567")
    await manager.close()
    restarted = QRLoginManager(database, gateway)
    assert restarted.state(await auth.authenticate(browser, csrf)) == "idle"
    with pytest.raises(ValueError, match="прерван"):
        await restarted.code(link, "12345")
    async with database.transaction() as session:
        saved = await session.scalar(select(AuthLink))
        assert saved.attempt_count == 1


async def test_successful_login_rejects_second_secret_submission(database, users, configured):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    try:
        await manager.phone(link, "+79991234567")
        assert await manager.code(link, "12345") == "connected"
        before = client.sign_in.await_count
        with pytest.raises(ValueError):
            await manager.code(link, "12345")
        with pytest.raises(ValueError):
            await manager.password(link, "private-password")
        assert client.sign_in.await_count == before
    finally:
        await manager.close()


def test_auth_secrets_are_redacted_from_structured_logs():
    from deadline_assistant.logging import redact

    values = {
        key: "private-value"
        for key in (
            "phone",
            "code",
            "phone_code_hash",
            "password",
            "session_string",
            "encrypted_session_data",
            "api_hash",
            "api_key",
            "cookie",
            "authorization",
            "token",
        )
    }
    values["error_type"] = "PhoneCodeInvalidError"
    redacted = redact(None, "", values)
    assert all(value == "[скрыто]" for key, value in redacted.items() if key != "error_type")
    assert redacted["error_type"] == "PhoneCodeInvalidError"


async def test_failed_old_session_revocation_warns_without_losing_new_connection(
    database, users, configured
):
    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    gateway.revoke = AsyncMock(return_value=False)
    manager.on_connected = AsyncMock()
    manager.on_revoke_failed = AsyncMock()
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("old-encrypted")
    try:
        await manager.phone(link, "+79991234567")
        assert await manager.code(link, "12345") == "connected"
        await asyncio.gather(*manager.finalizers)
        manager.on_connected.assert_awaited_once()
        manager.on_revoke_failed.assert_awaited_once()
        async with database.transaction() as session:
            assert (
                await TenantRepository(session, users[0].id).telegram_session()
            ).status == "connected"
    finally:
        await manager.close()


async def test_late_login_notification_is_suppressed_after_disconnect(database, users, configured):
    from deadline_assistant.services.accounts import AccountService

    auth, browser, csrf, link, gateway, client, manager = await login_context(
        database, users[0], configured
    )
    entered, finish = asyncio.Event(), asyncio.Event()

    async def revoke(user_id, encrypted):
        if encrypted == "old-encrypted":
            entered.set()
            await finish.wait()
        return True

    gateway.revoke = AsyncMock(side_effect=revoke)
    manager.on_connected = AsyncMock()
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).save_session("old-encrypted")
    try:
        await manager.phone(link, "+79991234567")
        assert await manager.code(link, "12345") == "connected"
        await entered.wait()
        assert await AccountService(database, gateway).disconnect(users[0].id)
        finish.set()
        await asyncio.gather(*manager.finalizers)
        manager.on_connected.assert_not_awaited()
    finally:
        finish.set()
        await manager.close()
