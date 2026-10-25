"""Local macOS Docker launcher. Host operations use only the Python standard library."""

from __future__ import annotations

import argparse
import asyncio
import fcntl
import hashlib
import os
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
PROJECT = "deadline-macos-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:10]


def run(
    arguments: list[str],
    *,
    capture: bool = False,
    check: bool = True,
    environment: dict[str, str] | None = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        arguments, cwd=ROOT, env=environment, text=True, capture_output=capture, check=check
    )


def environment() -> dict[str, str]:
    return {
        **os.environ,
        "LOCAL_UID": str(os.getuid()),
        "LOCAL_GID": str(os.getgid()),
        "LOCAL_DATA_DIR": str((ROOT / "data").resolve()),
    }


def compose(
    *arguments: str, capture: bool = False, check: bool = True
) -> subprocess.CompletedProcess[str]:
    return run(
        [
            "docker",
            "compose",
            "-p",
            PROJECT,
            "-f",
            str(ROOT / "compose.macos.yaml"),
            "--env-file",
            str(ROOT / ".env"),
            *arguments,
        ],
        capture=capture,
        check=check,
        environment=environment(),
    )


def ready() -> bool:
    return run(["docker", "info"], capture=True, check=False).returncode == 0


def ensure_docker() -> None:
    if ready():
        return
    print("Запускаю Docker Desktop…", flush=True)
    run(["/usr/bin/open", "-a", "Docker"])
    deadline = time.monotonic() + 120
    while time.monotonic() < deadline:
        if ready():
            return
        time.sleep(2)
    raise RuntimeError("Docker Desktop не запустился. Откройте его и дождитесь готовности.")


def trust_local_certificate(certificate: Path) -> None:
    verify = [
        "/usr/bin/security",
        "verify-cert",
        "-c",
        str(certificate),
        "-p",
        "ssl",
        "-n",
        "localhost",
        "-q",
    ]
    if run(verify, capture=True, check=False).returncode == 0:
        return
    print(
        "\nДобавляю доверие к сертификату Deadline Assistant только для HTTPS localhost.",
        flush=True,
    )
    print("Это сертификат сервера, а не центр сертификации. Срок действия — 90 дней.", flush=True)
    print(
        "macOS может запросить разрешение в системном окне. Пароль Telegram здесь не нужен.",
        flush=True,
    )
    keychain = Path.home() / "Library/Keychains/login.keychain-db"
    run(
        [
            "/usr/bin/security",
            "add-trusted-cert",
            "-r",
            "trustRoot",
            "-p",
            "ssl",
            "-s",
            "localhost",
            "-k",
            str(keychain),
            str(certificate),
        ]
    )
    if run(verify, capture=True, check=False).returncode:
        raise RuntimeError(
            "Доверие к localhost не подтверждено. Повторите start и разрешите действие macOS."
        )


def start() -> None:
    if not (ROOT / ".env").is_file():
        raise RuntimeError("Сначала заполните .env по README.md; существующие ключи не заменяются.")
    ensure_docker()
    data = ROOT / "data"
    data.mkdir(mode=0o700, exist_ok=True)
    with (data / ".macos-docker-launch.lock").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print("Запуск уже выполняется в другом окне.", flush=True)
            return
        existing = compose("ps", "--status", "running", "-q", "app", capture=True).stdout.strip()
        if existing:
            trust_local_certificate(data / "local-tls/localhost.crt")
            print("Бот уже запущен: https://localhost:8443", flush=True)
            return
        print("Собираю контейнер приложения…", flush=True)
        compose("build", "app")
        print("Готовлю локальный HTTPS…", flush=True)
        compose(
            "run",
            "--rm",
            "--no-deps",
            "app",
            "python",
            "scripts/generate_local_tls.py",
            str(data / "local-tls"),
        )
        trust_local_certificate(data / "local-tls/localhost.crt")
        print("Сохраняю резервную копию и проверяю настройки…", flush=True)
        compose("run", "--rm", "--no-deps", "app", "python", "scripts/macos_docker.py", "prepare")
        compose("up", "-d", "--no-build", "--wait", "--wait-timeout", "120", "app")
    print(
        "\nБот запущен в Docker. Откройте своего бота в Telegram на этом Mac и отправьте /start.",
        flush=True,
    )
    print("Ссылку подключения открывайте на этом же Mac: https://localhost:8443.", flush=True)
    print(
        "Остановка: stop.command. Закрытие окна и Ctrl+C останавливают только показ журналов.\n",
        flush=True,
    )
    compose("logs", "--follow", "--tail", "30", "app", check=False)


def stop() -> None:
    if not ready():
        print("Docker сейчас не работает; контейнер бота остановлен.", flush=True)
        return
    containers = run(
        [
            "docker",
            "ps",
            "--filter",
            f"label=com.docker.compose.project={PROJECT}",
            "--filter",
            "label=com.docker.compose.service=app",
            "-q",
        ],
        capture=True,
    ).stdout.split()
    if not containers:
        print("Бот уже остановлен.", flush=True)
        return
    print("Штатно останавливаю контейнер, данные сохраняются…", flush=True)
    run(["docker", "stop", *containers])
    print("Бот остановлен. База, настройки и сертификат сохранены.", flush=True)


async def prepare() -> None:
    # Executed inside the image: no Bot API, Telethon or LLM network calls.
    from pydantic import ValidationError
    from sqlalchemy.engine import make_url

    from deadline_assistant.config import Settings
    from deadline_assistant.db.session import Database
    from deadline_bot.cli import missing_settings

    try:
        settings = Settings()
    except ValidationError as exc:
        for error in exc.errors(include_input=False, include_url=False):
            print(error["msg"].removeprefix("Value error, "), file=sys.stderr)
        raise SystemExit(3) from None
    missing = missing_settings(
        settings,
        [
            "TELEGRAM_BOT_TOKEN",
            "TELEGRAM_API_ID",
            "TELEGRAM_API_HASH",
            "LOWROUTER_API_KEY",
            "SESSION_ENCRYPTION_KEY",
            "LLM_MODEL",
        ],
    )
    if missing:
        raise RuntimeError("Заполните в .env: " + ", ".join(missing))
    database = Database(settings)
    try:
        async with database.lock("server", wait=False):
            source = Path(make_url(settings.database_url.get_secret_value()).database or "")
            if await asyncio.to_thread(source.is_file):
                stamp = datetime.now(ZoneInfo(settings.default_timezone)).strftime(
                    "%Y-%m-%d_%H-%M-%S_%f"
                )
                backup = source.parent / "backups" / f"before-docker-start-{stamp}.sqlite"
                await asyncio.to_thread(
                    run, [sys.executable, "scripts/backup_sqlite.py", str(source), str(backup)]
                )
            await asyncio.to_thread(run, [sys.executable, "-m", "alembic", "upgrade", "head"])
            await asyncio.to_thread(run, [sys.executable, "-m", "deadline_bot", "check-config"])
    finally:
        await database.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Локальный Docker-запуск Deadline Assistant на macOS"
    )
    parser.add_argument("action", choices=["start", "stop", "prepare"])
    action = parser.parse_args().action
    try:
        if action == "prepare":
            asyncio.run(prepare())
        elif action == "start":
            start()
        else:
            stop()
    except KeyboardInterrupt:
        print(
            "\nПоказ журналов/подготовка завершены. "
            "Для остановки контейнера используйте stop.command."
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(
            f"Команда завершилась с кодом {exc.returncode}. Проверьте сообщение выше."
        ) from None
    except Exception as exc:
        if type(exc).__name__ == "JobBusy":
            message = (
                "Бот уже работает из другого терминала. Сначала остановите его там через Ctrl+C."
            )
        else:
            message = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        raise SystemExit("Не удалось выполнить действие: " + message) from None


if __name__ == "__main__":
    main()
