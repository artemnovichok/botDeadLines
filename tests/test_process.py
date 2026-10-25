import asyncio
import os
import signal
import socket
import sys
from pathlib import Path

import httpx

from deadline_assistant.config import Settings
from deadline_assistant.db.session import Database


async def test_real_http_process_and_graceful_sigterm(
    database: Database, settings: Settings, tmp_path: Path
) -> None:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url.get_secret_value(),
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "PUBLIC_BASE_URL": f"http://localhost:{port}",
        "APP_ENV": "development",
        "BOT_TOKEN": "",
        "TELEGRAM_BOT_TOKEN": "",
        "TELEGRAM_API_ID": "",
        "TELEGRAM_API_HASH": "",
        "LLM_API_KEY": "",
        "LOWROUTER_API_KEY": "",
        "LLM_MODEL": "",
        "SESSION_ENCRYPTION_KEY": "",
    }
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "deadline_assistant.main",
        env=env,
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        async with httpx.AsyncClient(
            base_url=f"http://localhost:{port}", trust_env=False
        ) as client:
            async with asyncio.timeout(10):
                while True:
                    try:
                        response = await client.get("/health/ready")
                        if response.status_code == 200:
                            break
                    except httpx.ConnectError:
                        pass
                    await asyncio.sleep(0.05)
            page = await client.get("/connect")
            assert page.status_code == 200
            assert "Подключите учебные чаты" in page.text
        process.send_signal(signal.SIGTERM)
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        # Uvicorn после graceful shutdown повторно поднимает исходный сигнал.
        assert process.returncode in (0, -signal.SIGTERM)
        assert "Приложение остановлено" in output.decode()
        assert "Traceback" not in output.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()


async def test_polling_failure_exits_server_for_restart(database, settings, tmp_path):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    env = {
        **os.environ,
        "DATABASE_URL": settings.database_url.get_secret_value(),
        "PUBLIC_BASE_URL": "https://localhost",
        "HOST": "127.0.0.1",
        "PORT": str(port),
        "APP_ENV": "development",
        "TELEGRAM_BOT_TOKEN": "123456:synthetic-mock-value",
        "SESSION_ENCRYPTION_KEY": settings.session_encryption_key.get_secret_value(),
        "TELEGRAM_API_ID": "",
        "TELEGRAM_API_HASH": "",
        "LOWROUTER_API_KEY": "",
        "LLM_API_KEY": "",
        "LLM_MODEL": "",
    }
    script = """
from aiogram import Dispatcher
from deadline_assistant.main import main
async def fail(*args, **kwargs):
    raise RuntimeError('simulated polling failure')
Dispatcher.start_polling = fail
main()
"""
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-c",
        script,
        env=env,
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        output, _ = await asyncio.wait_for(process.communicate(), timeout=10)
        assert process.returncode == 5, output.decode()
        assert "Опрос Telegram остановлен" in output.decode()
        assert "Приложение остановлено" in output.decode()
        assert "Traceback" not in output.decode()
    finally:
        if process.returncode is None:
            process.kill()
            await process.wait()
