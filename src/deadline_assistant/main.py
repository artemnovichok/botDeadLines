import os

import uvicorn
from pydantic import ValidationError

from deadline_assistant.api.app import create_app
from deadline_assistant.config import Settings
from deadline_assistant.logging import configure_logging


def main() -> None:
    os.umask(0o077)
    try:
        settings = Settings()
    except ValidationError:
        raise SystemExit(
            "Ошибка конфигурации. Проверьте переменные окружения по .env.example"
        ) from None
    configure_logging(settings.log_level)
    serve(settings)


def serve(settings: Settings) -> None:
    app = create_app(settings)
    configuration = uvicorn.Config(
        app,
        host=settings.host,
        port=settings.port,
        forwarded_allow_ips=settings.forwarded_allow_ips,
        access_log=False,
        log_config=None,
        log_level=settings.log_level.lower(),
        timeout_graceful_shutdown=settings.sync_timeout_seconds + 30,
        ssl_certfile=str(settings.tls_cert_file) if settings.tls_cert_file else None,
        ssl_keyfile=str(settings.tls_key_file) if settings.tls_key_file else None,
    )
    server = uvicorn.Server(configuration)
    runtime = app.state.runtime
    runtime.on_polling_failure = lambda: setattr(server, "should_exit", True)
    server.run()
    if runtime.polling_failed:
        raise SystemExit(5)  # Docker/systemd can restart the complete runtime.


if __name__ == "__main__":
    main()
