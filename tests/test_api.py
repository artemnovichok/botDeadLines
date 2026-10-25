from unittest.mock import AsyncMock

import httpx

from deadline_assistant.api.app import create_app
from deadline_assistant.config import Settings
from deadline_assistant.db.models import User
from deadline_assistant.db.session import Database
from deadline_assistant.runtime import Runtime


async def test_web_auth_security(
    database: Database, users: tuple[User, User], settings: Settings
) -> None:
    settings = Settings(
        _env_file=None, **{**settings.model_dump(), "public_base_url": "https://localhost:8000"}
    )
    runtime = Runtime(settings)
    await runtime.database.close()
    runtime.database = database
    runtime.auth.database = database
    runtime.login.database = database
    runtime.login.begin = AsyncMock(return_value="waiting")
    app = create_app(settings, runtime)
    token = await runtime.auth.issue(users[0].id)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="https://localhost:8000") as client:
        page = await client.get("/connect")
        assert page.status_code == 200
        assert 'lang="ru"' in page.text
        assert page.headers["cache-control"] == "no-store"
        assert "frame-ancestors 'none'" in page.headers["content-security-policy"]
        assert (await client.get("/api/auth/state")).status_code == 401
        assert (await client.post("/api/auth/exchange", json={"token": token})).status_code == 403
        response = await client.post(
            "/api/auth/exchange",
            json={"token": token},
            headers={"origin": "https://localhost:8000"},
        )
        assert response.status_code == 200
        assert "HttpOnly" in response.headers.get_list("set-cookie")[0]
        assert "SameSite=strict" in response.headers.get_list("set-cookie")[0]
        assert (
            await client.post("/api/auth/qr", json={}, headers={"origin": "https://localhost:8000"})
        ).status_code == 403
        csrf = client.cookies.get("deadline_csrf")
        headers = {"origin": "https://localhost:8000", "x-csrf-token": csrf}
        assert (await client.post("/api/auth/qr", json={}, headers=headers)).status_code == 200
        runtime.login.begin.assert_awaited_once()
        assert (
            await client.post(
                "/api/auth/qr", json={}, headers={**headers, "origin": "https://evil.example"}
            )
        ).status_code == 403
        invalid = await client.post(
            "/api/auth/password",
            json={"password": "sensitive-password", "extra": "value"},
            headers=headers,
        )
        assert invalid.status_code == 422
        assert "sensitive-password" not in invalid.text
        assert (
            await client.post("/api/auth/exchange", json={"token": token}, headers=headers)
        ).status_code == 401
    await runtime.analyzer.close()


async def test_lifespan_without_keys(database: Database, settings: Settings) -> None:
    runtime = Runtime(settings)
    app = create_app(settings, runtime)
    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8000"
        ) as client:
            assert (await client.get("/health/live")).status_code == 200
            assert (await client.get("/health/ready")).status_code == 200
        assert runtime.scheduler.scheduler.running
    assert not runtime.scheduler.scheduler.running


async def test_phone_endpoints_approval_csrf_secrets_and_cancel(database, users, configured):
    from types import SimpleNamespace
    from unittest.mock import MagicMock

    from telethon import types
    from telethon.errors import SessionPasswordNeededError

    configured = Settings(
        _env_file=None, **{**configured.model_dump(), "public_base_url": "https://localhost:8000"}
    )
    runtime = Runtime(configured)
    client_mock = AsyncMock()
    client_mock.session = SimpleNamespace(save=lambda: "session-for-browser-flow")
    client_mock.get_me.return_value = SimpleNamespace(id=101, bot=False)
    client_mock.send_code_request.return_value = types.auth.SentCode(
        type=types.auth.SentCodeTypeApp(length=5),
        phone_code_hash="secret-code-hash",
    )
    client_mock.sign_in.side_effect = [SessionPasswordNeededError(None), SimpleNamespace(id=101)]
    runtime.gateway.new_client = MagicMock(return_value=client_mock)
    app = create_app(configured, runtime)
    token = await runtime.auth.issue(users[0].id)
    origin = {"origin": "https://localhost:8000"}
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://localhost:8000"
        ) as client:
            assert (
                await client.post("/api/auth/exchange", json={"token": token}, headers=origin)
            ).status_code == 200
            assert (await client.get("/api/auth/state")).json()["state"] == "approval_required"
            headers = {**origin, "x-csrf-token": client.cookies.get("deadline_csrf")}
            denied = await client.post("/api/auth/qr", json={}, headers=headers)
            assert denied.status_code == 400
            client_mock.connect.assert_not_awaited()
            link = await runtime.auth.authenticate(client.cookies.get("deadline_auth"))
            await runtime.auth.approve(users[0].id, link.id)
            for endpoint, body in [
                ("phone", {"phone": "+79991234567"}),
                ("code", {"code": "12345"}),
                ("password", {"password": "secret"}),
                ("cancel", {}),
            ]:
                assert (
                    await client.post("/api/auth/" + endpoint, json=body, headers=origin)
                ).status_code == 403
            invalid = await client.post(
                "/api/auth/phone", json={"phone": "private-not-a-number"}, headers=headers
            )
            assert invalid.status_code == 422 and "private-not-a-number" not in invalid.text
            assert (
                await client.post(
                    "/api/auth/phone", json={"phone": "+79991234567"}, headers=headers
                )
            ).json()["state"] == "code_required"
            snapshot = (await client.get("/api/auth/state")).json()
            assert snapshot["state"] == "code_required"
            assert "secret-code-hash" not in str(snapshot)
            assert (
                await client.post("/api/auth/code", json={"code": "12345"}, headers=headers)
            ).json()["state"] == "password_required"
            assert (
                await client.post(
                    "/api/auth/password", json={"password": "private-2fa"}, headers=headers
                )
            ).json()["state"] == "connected"
            assert "session-for-browser-flow" not in (await client.get("/api/auth/state")).text
            assert (
                await client.post("/api/auth/cancel", json={}, headers=headers)
            ).status_code == 200
            assert (await client.get("/api/auth/state")).status_code == 401
            assert (
                await client.post("/api/auth/exchange", json={"token": token}, headers=origin)
            ).status_code == 401
    finally:
        await runtime.login.close()
        await runtime.analyzer.close()
        await runtime.database.close()


async def test_https_required_secure_cookies_and_rate_limit(database, users, configured):
    secure = Settings(
        _env_file=None,
        **{
            **configured.model_dump(),
            "public_base_url": "https://localhost",
            "app_env": "production",
        },
    )
    runtime = Runtime(secure)
    app = create_app(secure, runtime)
    token = await runtime.auth.issue(users[0].id)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="https://localhost"
        ) as client:
            response = await client.post(
                "/api/auth/exchange", json={"token": token}, headers={"origin": "https://localhost"}
            )
            assert response.status_code == 200
            assert all("Secure" in cookie for cookie in response.headers.get_list("set-cookie"))
            assert "max-age" in response.headers["strict-transport-security"]
            assert (await client.get("http://localhost/connect")).status_code == 426
            assert (
                await client.post("http://localhost/api/auth/phone", json={"phone": "+79991234567"})
            ).status_code == 426
            for _ in range(20):
                response = await client.post(
                    "/api/auth/exchange",
                    json={"token": "x" * 43},
                    headers={"origin": "https://localhost"},
                )
            assert response.status_code == 429
            assert int(response.headers["retry-after"]) > 0
    finally:
        await runtime.analyzer.close()
        await runtime.database.close()


async def test_cross_browser_csrf_and_cookie_isolation(database, users, settings):
    runtime = Runtime(settings)
    app = create_app(settings, runtime)
    try:
        async with (
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost:8000"
            ) as alice,
            httpx.AsyncClient(
                transport=httpx.ASGITransport(app), base_url="http://localhost:8000"
            ) as bob,
        ):
            origin = {"origin": "http://localhost:8000"}
            for client, user in [(alice, users[0]), (bob, users[1])]:
                token = await runtime.auth.issue(user.id)
                assert (
                    await client.post("/api/auth/exchange", json={"token": token}, headers=origin)
                ).status_code == 200
            response = await bob.post(
                "/api/auth/cancel",
                json={},
                headers={**origin, "x-csrf-token": alice.cookies.get("deadline_csrf")},
            )
            assert response.status_code == 401
            assert (await alice.get("/api/auth/state")).status_code == 200
            assert (await bob.get("/api/auth/state")).status_code == 200
    finally:
        await runtime.analyzer.close()
        await runtime.database.close()


async def test_secret_fields_refused_over_http_even_in_development(database, settings):
    runtime = Runtime(settings)
    app = create_app(settings, runtime)
    try:
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app), base_url="http://localhost:8000"
        ) as client:
            for endpoint, body in [
                ("phone", {"phone": "+79991234567"}),
                ("code", {"code": "12345"}),
                ("password", {"password": "secret"}),
            ]:
                response = await client.post("/api/auth/" + endpoint, json=body)
                assert response.status_code == 426
                assert body not in [response.json()]
    finally:
        await runtime.analyzer.close()
        await runtime.database.close()
