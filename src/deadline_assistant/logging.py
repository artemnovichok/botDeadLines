import logging
from collections.abc import MutableMapping
from typing import Any

import structlog


def redact(_: Any, __: str, event_dict: MutableMapping[str, Any]) -> MutableMapping[str, Any]:
    for key in list(event_dict):
        if any(
            part in key.lower()
            for part in (
                "token",
                "password",
                "secret",
                "session",
                "api_key",
                "api_hash",
                "phone",
                "code",
                "cookie",
                "authorization",
            )
        ):
            event_dict[key] = "[скрыто]"
    return event_dict


def configure_logging(level: str) -> None:
    logging.basicConfig(level=level, format="%(message)s", force=True)
    # Не пропускаем URL с Bot API токенами и тела запросов из сторонних библиотек.
    for name in (
        "aiogram",
        "telethon",
        "httpx",
        "httpcore",
        "openai",
        "sqlalchemy",
        "uvicorn.access",
        "uvicorn",
        "uvicorn.error",
        "apscheduler",
    ):
        logger = logging.getLogger(name)
        logger.disabled = True
        logger.handlers = [logging.NullHandler()]
        logger.propagate = False
        logger.setLevel(logging.CRITICAL + 1)
    structlog.configure(
        processors=[
            structlog.contextvars.merge_contextvars,
            redact,
            structlog.processors.add_log_level,
            structlog.processors.TimeStamper(fmt="iso", utc=True),
            structlog.processors.JSONRenderer(ensure_ascii=False),
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
        wrapper_class=structlog.make_filtering_bound_logger(getattr(logging, level)),
    )
