from unittest.mock import AsyncMock

import pytest

from deadline_assistant.domain import (
    EntityNotFound,
    EventData,
    IntegrationUnavailable,
    SessionExpired,
)
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.accounts import AccountService
from deadline_assistant.services.auth import AuthService


def dialog(chat_id, title="Приватная группа"):
    return {
        "id": chat_id,
        "title": title,
        "metadata": {"kind": "channel", "id": abs(chat_id), "access_hash": 5, "username": ""},
    }


async def setup_accounts(database, users):
    async with database.transaction() as session:
        for user in users:
            await TenantRepository(session, user.id).save_session(str(user.id))
    gateway = AsyncMock()
    gateway.dialogs.return_value = [dialog(-1001), dialog(-1002, "Канал")]
    gateway.revoke.return_value = True
    notifier = AsyncMock()
    return AccountService(database, gateway, notifier), gateway, notifier


async def test_multiple_selection_removal_and_user_isolation(database, users):
    service, gateway, notifier = await setup_accounts(database, users)
    assert await service.select(users[0].id, {-1001, -1002}) == 2
    assert await service.select(users[1].id, {-1001}) == 1
    chats = await service.connections(users[0].id)
    await service.untrack(users[0].id, chats[0].id)
    assert sum(c.enabled for c in await service.connections(users[0].id)) == 1
    assert sum(c.enabled for c in await service.connections(users[1].id)) == 1
    with pytest.raises(EntityNotFound):
        await service.untrack(users[1].id, chats[0].id)
    await service.select(users[0].id, set())
    assert not any(c.enabled for c in await service.connections(users[0].id))
    assert sum(c.enabled for c in await service.connections(users[1].id)) == 1


async def test_cannot_track_unknown_or_inaccessible_chat(database, users):
    service, gateway, notifier = await setup_accounts(database, users)
    await service.select(users[0].id, {-1001})
    gateway.dialogs.return_value = []
    with pytest.raises(IntegrationUnavailable):
        await service.select(users[0].id, {-1001, -1002})
    assert sum(c.enabled for c in await service.connections(users[0].id)) == 1
    with pytest.raises(EntityNotFound):
        await service.track(users[0].id, -123)


async def test_revocation_notifies_once_and_preserves_deadlines(database, users):
    service, gateway, notifier = await setup_accounts(database, users)
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Экзамен")
        )
    gateway.dialogs.side_effect = SessionExpired("Сессия отозвана")
    with pytest.raises(SessionExpired):
        await service.dialogs(users[0].id)
    with pytest.raises(IntegrationUnavailable):
        await service.dialogs(users[0].id)
    notifier.send.assert_awaited_once()
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.telegram_session()).status == "revoked"
        assert (await repo.event(event.id)).title == "Экзамен"
        assert (
            await TenantRepository(session, users[1].id).telegram_session()
        ).status == "connected"


async def test_temporary_api_error_does_not_revoke_or_delete_events(database, users):
    service, gateway, notifier = await setup_accounts(database, users)
    async with database.transaction() as session:
        await TenantRepository(session, users[0].id).create_event(EventData(title="Сдать работу"))
    gateway.dialogs.side_effect = IntegrationUnavailable("Сеть недоступна")
    with pytest.raises(IntegrationUnavailable):
        await service.dialogs(users[0].id)
    notifier.send.assert_not_awaited()
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert (await repo.telegram_session()).status == "connected"
        assert len(await repo.active_events()) == 1


@pytest.mark.parametrize("remote_revoked", [True, False])
async def test_disconnect_invalidates_links_chats_only_own_session(database, users, remote_revoked):
    service, gateway, notifier = await setup_accounts(database, users)
    gateway.revoke.return_value = remote_revoked
    await service.select(users[0].id, {-1001})
    await service.select(users[1].id, {-1002})
    auth = AuthService(database)
    browser, csrf = await auth.exchange(await auth.issue(users[0].id))
    async with database.transaction() as session:
        event = await TenantRepository(session, users[0].id).create_event(
            EventData(title="Сохранить")
        )
    assert await service.disconnect(users[0].id) == remote_revoked
    with pytest.raises(EntityNotFound):
        await auth.authenticate(browser, csrf)
    async with database.transaction() as session:
        repo = TenantRepository(session, users[0].id)
        assert await repo.telegram_session() is None
        assert await repo.chats() == []
        assert (await repo.event(event.id)).title == "Сохранить"
        assert len(await TenantRepository(session, users[1].id).chats()) == 1
        assert await TenantRepository(session, users[1].id).telegram_session() is not None
