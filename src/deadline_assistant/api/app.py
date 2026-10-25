from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from io import BytesIO
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import qrcode
import qrcode.image.svg
import structlog
from fastapi import FastAPI, HTTPException, Request, Response
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import Field, SecretStr, field_validator
from starlette.exceptions import HTTPException as StarletteHTTPException
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from deadline_assistant.config import Settings
from deadline_assistant.db.models import AuthLink
from deadline_assistant.domain import (
    AuthFlowError,
    EntityNotFound,
    IntegrationUnavailable,
    RateLimited,
    StrictModel,
)
from deadline_assistant.services.limits import RateLimiter

if TYPE_CHECKING:
    from deadline_assistant.runtime import Runtime

log = structlog.get_logger()


class BodyLimitMiddleware:
    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        size = 0

        async def limited_receive() -> Message:
            nonlocal size
            message = await receive()
            size += len(message.get("body", b""))
            if size > 8192:
                raise HTTPException(413, "Слишком большой запрос")
            return message

        await self.app(scope, limited_receive, send)


class ExchangeInput(StrictModel):
    token: SecretStr = Field(min_length=40, max_length=100)


class PasswordInput(StrictModel):
    password: SecretStr = Field(min_length=1, max_length=1024)


class PhoneInput(StrictModel):
    phone: SecretStr = Field(min_length=8, max_length=16)

    @field_validator("phone")
    @classmethod
    def international(cls, value: SecretStr) -> SecretStr:
        import re

        if not re.fullmatch(r"\+[1-9][0-9]{6,14}", value.get_secret_value()):
            raise ValueError("Нужен международный номер")
        return value


class CodeInput(StrictModel):
    code: SecretStr = Field(min_length=4, max_length=10)

    @field_validator("code")
    @classmethod
    def numeric(cls, value: SecretStr) -> SecretStr:
        if not value.get_secret_value().isascii() or not value.get_secret_value().isdigit():
            raise ValueError("Нужны цифры кода")
        return value


def create_app(settings: Settings | None = None, runtime: "Runtime | None" = None) -> FastAPI:
    from deadline_assistant.runtime import Runtime

    settings = settings or Settings()
    runtime = runtime or Runtime(settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        await runtime.start()
        try:
            yield
        finally:
            await runtime.stop()

    app = FastAPI(
        title="Deadline Assistant",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    app.state.runtime = runtime
    limiter = RateLimiter()
    app.add_middleware(BodyLimitMiddleware)
    app.add_middleware(
        TrustedHostMiddleware,
        allowed_hosts=[urlsplit(settings.public_base_url).hostname or "localhost", "127.0.0.1"],
    )

    @app.middleware("http")
    async def security(request: Request, call_next: Callable[..., Any]) -> Response:
        response: Response
        secret_input = request.url.path in {
            "/api/auth/phone",
            "/api/auth/code",
            "/api/auth/password",
        }
        connection_page = request.url.path.startswith("/api/auth") or request.url.path == "/connect"
        if request.url.scheme != "https" and (
            secret_input
            or (urlsplit(settings.public_base_url).scheme == "https" and connection_page)
        ):
            response = JSONResponse({"detail": "Для подключения обязателен HTTPS"}, status_code=426)
        else:
            response = await call_next(request)
        response.headers.update(
            {
                "Content-Security-Policy": "default-src 'none'; script-src 'self'; "
                "style-src 'self'; img-src 'self'; connect-src 'self'; base-uri 'none'; "
                "frame-ancestors 'none'; form-action 'self'",
                "X-Content-Type-Options": "nosniff",
                "X-Frame-Options": "DENY",
                "Referrer-Policy": "no-referrer",
                "Cache-Control": "no-store",
                "Permissions-Policy": "camera=(), microphone=(), geolocation=()",
            }
        )
        if settings.app_env == "production":
            response.headers["Strict-Transport-Security"] = "max-age=31536000"
        return response

    @app.exception_handler(RequestValidationError)
    async def invalid_input(request: Request, exc: RequestValidationError) -> JSONResponse:
        # Стандартный ответ Pydantic может содержать введённый пароль или токен.
        return JSONResponse({"detail": "Проверьте формат введённых данных"}, status_code=422)

    @app.exception_handler(StarletteHTTPException)
    async def http_error(request: Request, exc: StarletteHTTPException) -> JSONResponse:
        text = {404: "Страница не найдена", 405: "Метод не поддерживается"}.get(
            exc.status_code, str(exc.detail)
        )
        return JSONResponse({"detail": text}, status_code=exc.status_code, headers=exc.headers)

    @app.exception_handler(EntityNotFound)
    async def unauthorized(request: Request, exc: EntityNotFound) -> JSONResponse:
        return JSONResponse(
            {"detail": "Ссылка недействительна или истекла. Получите новую в боте"}, status_code=401
        )

    @app.exception_handler(IntegrationUnavailable)
    async def unavailable(request: Request, exc: IntegrationUnavailable) -> JSONResponse:
        return JSONResponse(
            {"detail": "Интеграция не настроена или временно недоступна"}, status_code=503
        )

    @app.exception_handler(RateLimited)
    async def limited(request: Request, exc: RateLimited) -> JSONResponse:
        return JSONResponse(
            {"detail": str(exc)}, status_code=429, headers={"Retry-After": str(exc.retry_after)}
        )

    @app.exception_handler(Exception)
    async def internal_error(request: Request, exc: Exception) -> JSONResponse:
        log.error("Ошибка веб-компонента", error_type=type(exc).__name__)
        return JSONResponse(
            {"detail": "Не удалось выполнить запрос. Повторите позже"}, status_code=500
        )

    def require_origin(request: Request) -> None:
        if request.headers.get("origin") != settings.public_base_url.rstrip("/"):
            raise HTTPException(403, "Недопустимый источник запроса")

    async def authenticate(request: Request, mutate: bool = False) -> AuthLink:
        browser = request.cookies.get("deadline_auth")
        if not browser:
            raise EntityNotFound
        csrf = None
        if mutate:
            require_origin(request)
            csrf = request.headers.get("x-csrf-token")
            if not csrf:
                raise HTTPException(403, "Отсутствует защита CSRF")
        return await runtime.auth.authenticate(browser, csrf)

    @app.get("/health/live")
    async def live() -> dict[str, str]:
        return {"status": "работает"}

    @app.get("/health/ready")
    async def ready() -> dict[str, str]:
        await runtime.database.check()
        if runtime.bot_task and runtime.bot_task.done():
            raise HTTPException(503, "Опрос Telegram остановлен")
        return {"status": "готово"}

    @app.get("/connect")
    async def connect() -> FileResponse:
        return FileResponse(settings.static_dir / "connect.html")

    @app.post("/api/auth/exchange")
    async def exchange(data: ExchangeInput, request: Request, response: Response) -> dict[str, str]:
        require_origin(request)
        limiter.check("exchange-global", limit=300, seconds=60)
        limiter.check(
            "exchange:" + (request.client.host if request.client else "unknown"),
            limit=20,
            seconds=60,
        )
        browser, csrf = await runtime.auth.exchange(data.token.get_secret_value())
        secure = urlsplit(settings.public_base_url).scheme == "https"
        response.set_cookie(
            "deadline_auth",
            browser,
            httponly=True,
            secure=secure,
            samesite="strict",
            max_age=600,
            path="/api/auth",
        )
        response.set_cookie(
            "deadline_csrf",
            csrf,
            httponly=False,
            secure=secure,
            samesite="strict",
            max_age=600,
            path="/connect",
        )
        return {"status": "готово"}

    @app.post("/api/auth/qr")
    async def start_qr(request: Request) -> dict[str, str]:
        link = await authenticate(request, mutate=True)
        try:
            return {"state": await runtime.login.begin(link)}
        except AuthFlowError as exc:
            raise HTTPException(400, str(exc)) from None

    @app.get("/api/auth/state")
    async def state(request: Request) -> dict[str, str]:
        link = await authenticate(request)
        pending = runtime.login.pending.get(link.id)
        return {
            "state": runtime.login.state(link),
            "request_tag": link.id.hex[:6].upper(),
            "delivery": pending.delivery if pending else "",
        }

    @app.get("/api/auth/qr.svg")
    async def qr_image(request: Request) -> Response:
        link = await authenticate(request)
        url = runtime.login.qr_url(link.id)
        code = qrcode.make(url, image_factory=qrcode.image.svg.SvgPathImage)
        buffer = BytesIO()
        code.save(buffer)
        return Response(buffer.getvalue(), media_type="image/svg+xml")

    @app.post("/api/auth/password")
    async def password(data: PasswordInput, request: Request) -> dict[str, str]:
        link = await authenticate(request, mutate=True)
        try:
            result = await runtime.login.password(link, data.password.get_secret_value())
        except AuthFlowError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"state": result}

    @app.post("/api/auth/phone")
    async def phone(data: PhoneInput, request: Request) -> dict[str, str]:
        link = await authenticate(request, mutate=True)
        try:
            result = await runtime.login.phone(link, data.phone.get_secret_value())
        except AuthFlowError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"state": result}

    @app.post("/api/auth/code")
    async def code(data: CodeInput, request: Request) -> dict[str, str]:
        link = await authenticate(request, mutate=True)
        try:
            result = await runtime.login.code(link, data.code.get_secret_value())
        except AuthFlowError as exc:
            raise HTTPException(400, str(exc)) from None
        return {"state": result}

    @app.post("/api/auth/cancel")
    async def cancel(request: Request, response: Response) -> dict[str, str]:
        await runtime.login.cancel(await authenticate(request, mutate=True))
        response.delete_cookie("deadline_auth", path="/api/auth")
        response.delete_cookie("deadline_csrf", path="/connect")
        return {"state": "cancelled"}

    app.mount("/static", StaticFiles(directory=settings.static_dir), name="static")
    return app
