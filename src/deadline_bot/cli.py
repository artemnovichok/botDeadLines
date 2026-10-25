import argparse
import asyncio
import json
import os
import sys
from collections.abc import Sequence
from typing import Any, Never

from pydantic import ValidationError

from deadline_assistant.config import Settings
from deadline_assistant.db.schema import check_schema
from deadline_assistant.db.session import Database
from deadline_assistant.domain import EntityNotFound, IntegrationUnavailable, MessageData, utcnow
from deadline_assistant.integrations.llm import OpenAIAnalyzer
from deadline_assistant.logging import configure_logging
from deadline_assistant.services.cycle import CycleResult

EXIT_OK, EXIT_USAGE, EXIT_CONFIG, EXIT_PARTIAL, EXIT_FAILURE = 0, 2, 3, 4, 5


class Parser(argparse.ArgumentParser):
    def __init__(self, prog: str | None = None, **kwargs: Any) -> None:
        kwargs["add_help"] = False
        super().__init__(prog=prog, **kwargs)
        self.add_argument("-h", "--help", action="help", help="Показать справку и выйти")
        self._positionals.title = "Команды"
        self._optionals.title = "Параметры"

    def error(self, message: str) -> Never:
        self.print_usage(sys.stderr)
        self.exit(EXIT_USAGE, f"Ошибка аргументов: {message}\nИспользуйте --help.\n")


def positive_id(value: str) -> int:
    try:
        number = int(value)
        if not 0 < number < 2**63:
            raise ValueError
        return number
    except ValueError:
        raise argparse.ArgumentTypeError("Нужен положительный числовой Telegram user ID") from None


def parser() -> argparse.ArgumentParser:
    root = Parser(prog="python -m deadline_bot", description="Управление Deadline Assistant")
    root.add_argument("--env-file", default=".env", help="Файл настроек (по умолчанию .env)")
    commands = root.add_subparsers(dest="command", required=True, title="Команды")
    cycle = commands.add_parser(
        "run-cycle", help="Собрать сообщения и выполнить общий цикл анализа"
    )
    scope = cycle.add_mutually_exclusive_group(required=True)
    scope.add_argument(
        "--all", action="store_true", help="Все пользователи с подключённым Telegram"
    )
    scope.add_argument(
        "--user", type=positive_id, help="Числовой Telegram user ID одного пользователя"
    )
    mode = cycle.add_mutually_exclusive_group()
    mode.add_argument(
        "--send-now", action="store_true", help="Сразу отправить актуальную сводку ботом"
    )
    mode.add_argument(
        "--dry-run", action="store_true", help="Показать изменения во временной БД без отправки"
    )
    summary = commands.add_parser("send-summary", help="Отправить текущую сводку из БД без LLM")
    summary.add_argument(
        "--user", required=True, type=positive_id, help="Числовой Telegram user ID"
    )
    commands.add_parser(
        "check-config", help="Проверить настройки и версию схемы БД без внешних запросов"
    )
    commands.add_parser("test-llm", help="Выполнить небольшой реальный платный запрос к модели")
    commands.add_parser("serve", help="Запустить веб-компонент, бота и планировщик")
    return root


def missing_settings(settings: Settings, names: Sequence[str]) -> list[str]:
    mapping = {
        "TELEGRAM_BOT_TOKEN": settings.bot_token,
        "TELEGRAM_API_ID": settings.telegram_api_id,
        "TELEGRAM_API_HASH": settings.telegram_api_hash,
        "LOWROUTER_API_KEY": settings.llm_api_key,
        "SESSION_ENCRYPTION_KEY": settings.session_encryption_key,
        "LLM_MODEL": settings.llm_model,
    }
    return [name for name in names if not mapping[name]]


def print_result(result: CycleResult) -> None:
    prefix = f"Пользователь {result.telegram_user_id}: "
    if result.status == "partial":
        print(prefix + "обработана часть истории; повторный запуск продолжит с checkpoint.")
    if result.error:
        reason = {
            "JobBusy": "Задание уже выполняется другим процессом",
            "InputTooLarge": "Сообщение или память не помещаются в бюджет модели",
            "OutputTooLarge": "Ответ ИИ не поместился в LLM_MAX_OUTPUT_TOKENS",
            "SessionExpired": "Сессия Telegram отозвана; подключите аккаунт заново",
            "RateLimited": "Telegram ограничил частоту запросов; повторите позже",
            "TimeoutError": "Истекло время ожидания; сохранённые порции не потеряны",
            "AnalysisIdleTimeout": "Нет прогресса; следующий запуск продолжит сохранённый анализ",
            "IntegrationUnavailable": "Проверьте подключение Telegram и доступ к выбранным чатам",
            "TransientIntegrationError": "Telegram временно недоступен",
            "AuthenticationError": "Проверьте ключ и права доступа к LLM API",
            "RateLimitError": "Проверьте баланс и квоты LLM API",
            "APITimeoutError": "LLM API не ответил вовремя",
        }.get(result.error, "Повторите запуск; при повторной ошибке проверьте сеть, БД и журналы")
        print(prefix + f"ошибка ({result.error}). {reason}.")
        return
    print(
        prefix + f"сообщений {result.messages}, новых событий {result.events}, "
        f"конфликтов {result.conflicts}, длительность {result.duration_seconds:.2f} с."
    )
    print(
        "Расход API: "
        + json.dumps(result.usage, ensure_ascii=False)
        + "; стоимость не вычисляется — подтверждённые тарифы не заданы."
    )
    for proposal in result.proposals:
        print(json.dumps(proposal, ensure_ascii=False))


async def execute(args: argparse.Namespace, settings: Settings) -> int:
    if args.command == "test-llm":
        if not settings.llm_enabled:
            print("Задайте LOWROUTER_API_KEY и LLM_MODEL.")
            return EXIT_CONFIG
        print(f"Проверяю модель {settings.llm_model}. Запрос может быть платным.")
        analyzer = OpenAIAnalyzer(settings)
        try:
            output = await analyzer.analyze(
                [
                    MessageData(
                        chat_id=0,
                        message_id=1,
                        timestamp=utcnow(),
                        text="Всем хорошего дня. Учебных событий нет.",
                    )
                ],
                [],
                settings.default_timezone,
            )
            print(
                "API работает; ответ прошёл проверку схемы. Расход: "
                + json.dumps(output.token_usage, ensure_ascii=False)
            )
            return EXIT_OK
        finally:
            await analyzer.close()

    if args.command == "check-config":
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
            print("Не заполнены обязательные настройки: " + ", ".join(missing))
        database = Database(settings, readonly=True)
        try:
            try:
                await check_schema(database)
                print("БД доступна, версия миграций актуальна.")
            except Exception:
                print(
                    "БД недоступна или схема устарела. Проверьте DATABASE_URL; "
                    "выполните alembic upgrade head."
                )
                return EXIT_CONFIG
        finally:
            await database.close()
        print("Часовой пояс: " + settings.default_timezone)
        print(
            f"Ночной сбор: {settings.night_sync_time:%H:%M}; "
            f"утренняя сводка: {settings.default_digest_time:%H:%M}."
        )
        print(
            "HTTPS: "
            + (
                "настроен в URL; сертификат проверьте вручную"
                if settings.public_base_url.startswith("https://")
                else "для авторизации нужен HTTPS"
            )
        )
        return EXIT_CONFIG if missing else EXIT_OK

    from deadline_assistant.runtime import Runtime

    if args.command == "run-cycle":
        missing = missing_settings(
            settings,
            [
                "TELEGRAM_API_ID",
                "TELEGRAM_API_HASH",
                "SESSION_ENCRYPTION_KEY",
                "LOWROUTER_API_KEY",
                "LLM_MODEL",
            ],
        )
        if args.send_now and not settings.bot_token:
            missing.append("TELEGRAM_BOT_TOKEN")
        if missing:
            print("Заполните настройки: " + ", ".join(missing))
            return EXIT_CONFIG
    elif not settings.bot_token:
        print("Задайте TELEGRAM_BOT_TOKEN для отправки сводки.")
        return EXIT_CONFIG
    runtime = Runtime(settings, readonly=args.command == "run-cycle" and args.dry_run)
    try:
        await check_schema(runtime.database)
        users = await runtime.cycle.users(args.user)
        if args.command == "send-summary":
            await runtime.cycle.summary.send_current(users[0].id)
            print(f"Текущая сводка отправлена пользователю {args.user}. LLM не вызывался.")
            return EXIT_OK
        if args.dry_run:
            print(
                "Dry-run: реальные запросы к LLM возможны; "
                "БД и checkpoint не меняются, уведомлений не будет."
            )
        print(f"Подключённых пользователей для обработки: {len(users)}")
        results = await runtime.cycle.run_many(
            users,
            send_now=args.send_now,
            dry_run=args.dry_run,
            on_result=print_result,
        )
        failures = sum(r.error is not None for r in results)
        partials = sum(r.status == "partial" for r in results)
        print(
            f"Цикл завершён: успешно {len(results) - failures - partials}, "
            f"частично {partials}, ошибок {failures}."
        )
        return EXIT_PARTIAL if failures or partials else EXIT_OK
    finally:
        await runtime.login.close()
        await runtime.analyzer.close()
        if runtime.bot:
            await runtime.bot.session.close()
        await runtime.database.close()


def main(argv: Sequence[str] | None = None) -> None:
    os.umask(0o077)
    args = parser().parse_args(argv)
    try:
        settings = Settings(_env_file=args.env_file)
        configure_logging(settings.log_level)
        if args.command == "serve":
            from deadline_assistant.main import serve

            serve(settings)
            return
        code = asyncio.run(execute(args, settings))
    except ValidationError as exc:
        details = "; ".join(
            (".".join(str(p) for p in error["loc"]) or "настройки")
            + ": "
            + error["msg"].removeprefix("Value error, ")
            for error in exc.errors(include_input=False, include_url=False)
        )
        print(
            f"Ошибка конфигурации: {details}. Проверьте .env.example; секреты не выводятся.",
            file=sys.stderr,
        )
        code = EXIT_CONFIG
    except EntityNotFound:
        print(
            "Пользователь не найден или отключён. Сначала отправьте боту /start.", file=sys.stderr
        )
        code = EXIT_USAGE
    except IntegrationUnavailable:
        print("Интеграция недоступна. Проверьте настройки и подключение аккаунта.", file=sys.stderr)
        code = EXIT_FAILURE
    except KeyboardInterrupt:
        print(
            "Операция прервана. Следующий запуск продолжит с сохранённого checkpoint.",
            file=sys.stderr,
        )
        code = 130
    except Exception as exc:
        print(
            f"Ошибка выполнения ({type(exc).__name__}). Проверьте настройки, сеть и миграции.",
            file=sys.stderr,
        )
        code = EXIT_FAILURE
    raise SystemExit(code)
