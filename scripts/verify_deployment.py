"""Isolated Docker Compose smoke tests without reading the user's .env or real API keys."""

import argparse
import json
import os
import secrets
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
IMAGE = "deadline-assistant:verification"


def run(*arguments: str, env: dict[str, str] | None = None) -> str:
    result = subprocess.run(arguments, capture_output=True, text=True, env=env, check=False)
    if result.returncode:
        # Command arguments never include passwords or API keys.
        raise RuntimeError(result.stderr.strip() or result.stdout.strip())
    return result.stdout.strip()


def verify(*, postgres: bool, pytest_postgres: bool) -> None:
    # Do not inherit credentials, DATABASE_URL or Compose settings from the calling shell.
    environment = {
        key: value
        for key, value in os.environ.items()
        if key in {"PATH", "HOME", "DOCKER_HOST", "DOCKER_CONTEXT", "TMPDIR"}
    }
    with tempfile.TemporaryDirectory(prefix="deadline-deploy-") as directory:
        root = Path(directory)
        password = secrets.token_hex(24)
        env_file = root / ".env"
        env_file.write_text(
            "APP_ENV=development\nPUBLIC_BASE_URL=http://localhost:8000\n"
            f"POSTGRES_PASSWORD={password}\n"
        )
        env_file.chmod(0o600)
        # Copy the templates so every relative env_file resolves inside the private directory.
        for name in ("docker-compose.yml", "compose.postgres.yaml"):
            (root / name).write_text((ROOT / name).read_text())
        sources = ["-f", str(root / "docker-compose.yml")]
        if postgres:
            sources += ["-f", str(root / "compose.postgres.yaml")]
        raw = run(
            "docker",
            "compose",
            "--project-directory",
            directory,
            "--env-file",
            str(env_file),
            *sources,
            "config",
            "--format",
            "json",
            env=environment,
        )
        config = json.loads(raw)
        app = config["services"]["app"]
        app.pop("build", None)
        app["image"] = IMAGE
        app["ports"] = []  # Inspect health inside the container; no application ports exposed.
        app["restart"] = "no"
        app["stop_grace_period"] = "15s"
        app["healthcheck"] = {
            "test": [
                "CMD",
                "python",
                "-c",
                "import urllib.request; "
                "urllib.request.urlopen('http://127.0.0.1:8000/health/ready', timeout=3)",
            ],
            "interval": "1s",
            "timeout": "4s",
            "retries": 30,
        }
        if postgres:
            config["services"]["postgres"]["ports"] = [
                {"target": 5432, "host_ip": "127.0.0.1", "published": "0", "protocol": "tcp"}
            ]
        project = "deadline-verify-" + uuid4().hex[:10]
        # Never reuse volumes from another project, including the user's installation.
        for name, volume in config.get("volumes", {}).items():
            volume["name"] = f"{project}_{name}"
        config_file = root / "compose.json"
        config_file.write_text(json.dumps(config))
        config_file.chmod(0o600)
        base = [
            "docker",
            "compose",
            "--project-directory",
            directory,
            "--env-file",
            str(env_file),
            "-p",
            project,
            "-f",
            str(config_file),
        ]

        def compose(*arguments: str) -> str:
            return run(*base, *arguments, env=environment)

        try:
            compose("config", "--quiet")
            compose("up", "-d", "--wait", "--wait-timeout", "90")
            compose("exec", "-T", "app", "alembic", "check")
            compose("exec", "-T", "app", "python", "-m", "deadline_bot", "run-cycle", "--help")
            compose(
                "exec",
                "-T",
                "app",
                "python",
                "-c",
                "import os, deadline_bot, deadline_assistant; "
                "from pathlib import Path; "
                "assert os.getuid() == 10001; "
                "assert not Path('/app/.env').exists(); "
                "assert not os.getenv('TELEGRAM_BOT_TOKEN'); "
                "assert not os.getenv('LOWROUTER_API_KEY')",
            )
            if not postgres:
                compose(
                    "exec",
                    "-T",
                    "app",
                    "python",
                    "scripts/backup_sqlite.py",
                    "/app/data/deadline.db",
                    "/app/data/backup.sqlite",
                )
            compose("restart", "app")
            app_id = compose("ps", "-q", "app")
            deadline = time.monotonic() + 60
            while time.monotonic() < deadline:
                status = run("docker", "inspect", "--format", "{{.State.Health.Status}}", app_id)
                if status == "healthy":
                    break
                time.sleep(1)
            else:
                raise RuntimeError("Контейнер не восстановил healthcheck после рестарта")
            if postgres and pytest_postgres:
                binding = compose("port", "postgres", "5432")
                port = binding.rsplit(":", 1)[1]
                test_env = {
                    **environment,
                    "POSTGRES_TEST_URL": f"postgresql+asyncpg://deadline:{password}@127.0.0.1:{port}/deadline",
                }
                output = run(
                    sys.executable,
                    "-m",
                    "pytest",
                    str(ROOT / "tests/test_postgres.py"),
                    "-q",
                    env=test_env,
                )
                print(output, flush=True)
            compose("stop", "app")
            logs = compose("logs", "--no-color", "app")
            if "Приложение остановлено" not in logs or "Traceback" in logs:
                raise RuntimeError("Нештатное завершение контейнера: " + logs[-4000:])
            print(
                f"{'PostgreSQL' if postgres else 'SQLite'}: Compose, миграции, healthcheck, "
                "UID 10001, CLI, рестарт и завершение — успешно.",
                flush=True,
            )
        except Exception:
            print(compose("logs", "--no-color", "app")[-4000:], file=sys.stderr)
            raise
        finally:
            compose("down", "--volumes", "--remove-orphans")


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(
        description="Изолированная проверка Docker, SQLite и PostgreSQL"
    )
    parser.add_argument(
        "--skip-build", action="store_true", help="Использовать уже собранный verification-образ"
    )
    parser.add_argument(
        "--pytest-postgres",
        action="store_true",
        help="Также запустить pytest на временном PostgreSQL",
    )
    args = parser.parse_args()
    if not args.skip_build:
        run("docker", "build", "-t", IMAGE, str(ROOT))
    verify(postgres=False, pytest_postgres=False)
    verify(postgres=True, pytest_postgres=args.pytest_postgres)


if __name__ == "__main__":
    main()
