import argparse
import asyncio
import sys
from unittest.mock import AsyncMock, patch

import pytest
from pydantic import SecretStr
from test_operations import pipeline, prepare

from deadline_assistant.config import Settings
from deadline_assistant.domain import AnalysisOutput, AnalysisResult
from deadline_assistant.runtime import Runtime
from deadline_assistant.services.cycle import CycleService
from deadline_bot.cli import execute, main, parser


@pytest.mark.parametrize(
    "command", [[], ["run-cycle"], ["send-summary"], ["check-config"], ["test-llm"], ["serve"]]
)
async def test_all_help_works_without_reading_env(command, tmp_path):
    process = await asyncio.create_subprocess_exec(
        sys.executable,
        "-m",
        "deadline_bot",
        *command,
        "--help",
        cwd=tmp_path,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    output, _ = await process.communicate()
    assert process.returncode == 0 and "Показать справку" in output.decode()


@pytest.mark.parametrize(
    "arguments",
    [
        ["run-cycle"],
        ["run-cycle", "--all", "--user", "123"],
        ["run-cycle", "--all", "--send-now", "--dry-run"],
        ["send-summary", "--user", "abc"],
        ["run-cycle", "--user", "-1"],
    ],
)
def test_argument_validation(arguments):
    with pytest.raises(SystemExit) as exc:
        parser().parse_args(arguments)
    assert exc.value.code == 2


async def test_config_offline_and_schema_checked(database, configured, capsys):
    settings = configured.model_copy(update={"bot_token": SecretStr("not-used-offline")})
    # A model_copy is sufficient: no bot or external connection is constructed.
    assert await execute(argparse.Namespace(command="check-config"), settings) == 0
    assert "версия миграций актуальна" in capsys.readouterr().out


async def test_missing_config_returns_code_three_and_does_not_create_database(
    tmp_path, settings, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    settings = settings.model_copy(
        update={"database_url": SecretStr("sqlite+aiosqlite:///missing/new.db")}
    )
    assert await execute(argparse.Namespace(command="check-config"), settings) == 3
    assert not (tmp_path / "missing").exists()


async def test_test_llm_uses_configured_analyzer_and_closes_it(configured, capsys):
    analyzer = AsyncMock(model="mock")
    analyzer.analyze.return_value = AnalysisOutput(
        result=AnalysisResult(), token_usage={"prompt_tokens": 7}
    )
    with patch("deadline_bot.cli.OpenAIAnalyzer", return_value=analyzer):
        assert await execute(argparse.Namespace(command="test-llm"), configured) == 0
    analyzer.analyze.assert_awaited_once()
    analyzer.close.assert_awaited_once()
    assert "API работает" in capsys.readouterr().out


def test_required_environment_names_and_legacy_aliases(monkeypatch, tmp_path):
    from cryptography.fernet import Fernet

    monkeypatch.chdir(tmp_path)
    for name in ("BOT_TOKEN", "LLM_API_KEY", "TELEGRAM_BOT_TOKEN", "LOWROUTER_API_KEY"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://localhost")
    monkeypatch.setenv("SESSION_ENCRYPTION_KEY", Fernet.generate_key().decode())
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "synthetic-bot-value")
    monkeypatch.setenv("LOWROUTER_API_KEY", "synthetic-llm-value")
    monkeypatch.setenv("DAILY_SYNC_HOUR", "3")
    monkeypatch.setenv("DAILY_DIGEST_HOUR", "9")
    settings = Settings(_env_file=None)
    assert settings.bot_token.get_secret_value() == "synthetic-bot-value"
    assert settings.llm_api_key.get_secret_value() == "synthetic-llm-value"
    assert settings.night_sync_time.hour == 3 and settings.default_digest_time.hour == 9
    monkeypatch.delenv("TELEGRAM_BOT_TOKEN")
    monkeypatch.setenv("BOT_TOKEN", "legacy-bot-value")
    assert Settings(_env_file=None).bot_token.get_secret_value() == "legacy-bot-value"


@pytest.mark.parametrize("command", ["run-cycle", "send-summary"])
async def test_cli_executes_real_services_with_mock_external_apis(
    command,
    configured,
    database,
    users,
    capsys,
):
    await prepare(database, users[0])
    configured = configured.model_copy(
        update={
            "bot_token": SecretStr("123456:synthetic-test-value"),
            "public_base_url": "https://localhost",
        }
    )
    runtime = Runtime(configured)
    await runtime.analyzer.close()
    sync, analyzer = pipeline(runtime.database, configured)
    runtime.sync, runtime.analyzer = sync, analyzer
    notifier = AsyncMock()
    runtime.cycle = CycleService(runtime.database, configured, sync, notifier)
    arguments = argparse.Namespace(
        command=command, user=101, all=False, dry_run=False, send_now=True
    )
    try:
        with patch("deadline_assistant.runtime.Runtime", return_value=runtime):
            assert await execute(arguments, configured) == 0
        notifier.send.assert_awaited_once()
        if command == "send-summary":
            analyzer.analyze.assert_not_awaited()
            assert "LLM не вызывался" in capsys.readouterr().out
        else:
            analyzer.analyze.assert_awaited_once()
            assert "успешно 1, частично 0, ошибок 0" in capsys.readouterr().out
    finally:
        await runtime.bot.session.close()


async def test_cli_bulk_error_code_keeps_successful_user(configured, database, users, capsys):
    for user in users:
        await prepare(database, user)
    runtime = Runtime(configured)
    await runtime.analyzer.close()
    sync, analyzer = pipeline(runtime.database, configured)
    normal = sync.sync_user

    async def one_error(user_id):
        if user_id == users[0].id:
            raise TimeoutError()
        return await normal(user_id)

    sync.sync_user = AsyncMock(side_effect=one_error)
    runtime.sync, runtime.analyzer = sync, analyzer
    runtime.cycle = CycleService(runtime.database, configured, sync, None)
    args = argparse.Namespace(
        command="run-cycle", user=None, all=True, dry_run=False, send_now=False
    )
    with patch("deadline_assistant.runtime.Runtime", return_value=runtime):
        assert await execute(args, configured) == 4
    assert "успешно 1, частично 0, ошибок 1" in capsys.readouterr().out


async def test_operator_announces_to_everyone_before_analysis(configured, database, users):
    for user in users:
        await prepare(database, user)
    configured = configured.model_copy(
        update={"bot_token": SecretStr("123456:synthetic-test-value")}
    )
    runtime = Runtime(configured)
    await runtime.analyzer.close()
    sync, analyzer = pipeline(runtime.database, configured)
    runtime.sync, runtime.analyzer = sync, analyzer
    timeline = []

    async def announce(user_id, text):
        timeline.append(("notice", user_id))

    original_sync = sync.sync_user

    async def analyze(user_id):
        timeline.append(("analysis", user_id))
        return await original_sync(user_id)

    sync.sync_user = AsyncMock(side_effect=analyze)
    notifier = AsyncMock()
    notifier.send.side_effect = announce
    runtime.cycle = CycleService(runtime.database, configured, sync, notifier)
    args = parser().parse_args(["run-cycle", "--all", "--announce", "Внеплановый тест"])
    with patch("deadline_assistant.runtime.Runtime", return_value=runtime):
        assert await execute(args, configured) == 0
    assert [kind for kind, _ in timeline] == ["notice", "notice", "analysis", "analysis"]
    assert {user_id for _, user_id in timeline[:2]} == {101, 202}


@pytest.mark.parametrize("text,dry_run", [("", False), ("Предупреждение", True)])
async def test_invalid_announcement_never_starts_runtime(configured, text, dry_run):
    args = argparse.Namespace(command="run-cycle", announce=text, dry_run=dry_run)
    with patch("deadline_assistant.runtime.Runtime") as factory:
        assert await execute(args, configured) == 2
    factory.assert_not_called()


async def test_cli_partial_cycle_reports_incomplete_and_returns_code_four(
    configured, database, users, capsys
):
    await prepare(database, users[0])
    configured = configured.model_copy(
        update={
            "sync_batch_size": 1,
            "sync_max_total_tokens": 1,
        }
    )
    runtime = Runtime(configured)
    await runtime.analyzer.close()
    sync, analyzer = pipeline(runtime.database, configured)
    first = sync.reader.fetch.return_value[0]
    sync.reader.fetch.return_value = [first, first.model_copy(update={"message_id": 2})]
    runtime.sync, runtime.analyzer = sync, analyzer
    runtime.cycle = CycleService(runtime.database, configured, sync, None)
    args = argparse.Namespace(
        command="run-cycle",
        user=101,
        all=False,
        dry_run=False,
        send_now=False,
    )
    try:
        with patch("deadline_assistant.runtime.Runtime", return_value=runtime):
            assert await execute(args, configured) == 4
        analyzer.analyze.assert_awaited_once()
        assert "успешно 0, частично 1, ошибок 0" in capsys.readouterr().out
    finally:
        await runtime.database.close()


def test_config_failure_explains_https_without_disclosing_secrets(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)
    settings_file = tmp_path / "private.env"
    settings_file.write_text(
        "TELEGRAM_BOT_TOKEN=123456:private-marker\nPUBLIC_BASE_URL=http://localhost:8000\n"
    )
    with pytest.raises(SystemExit) as exc:
        main(["--env-file", str(settings_file), "check-config"])
    assert exc.value.code == 3
    error = capsys.readouterr().err
    assert "PUBLIC_BASE_URL с HTTPS" in error
    assert "private-marker" not in error
