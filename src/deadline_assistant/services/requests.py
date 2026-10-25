import asyncio
from collections.abc import Awaitable, Callable
from datetime import timedelta

from aiogram.client.session.middlewares.base import BaseRequestMiddleware
from aiogram.exceptions import TelegramNetworkError, TelegramRetryAfter
from sqlalchemy import select

from deadline_assistant.config import Settings
from deadline_assistant.db.models import RequestBudget, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import RateLimited, TransientIntegrationError, utcnow


async def throttle(database: Database, key: str, interval: float) -> None:
    # Reserve an API time slot transactionally, then wait outside the transaction.
    async with database.transaction() as session:
        if database.is_sqlite:
            from sqlalchemy.dialects.sqlite import insert
        else:
            from sqlalchemy.dialects.postgresql import insert  # type: ignore[assignment]
        now = utcnow()
        await session.execute(
            insert(RequestBudget).values(key=key, next_at=now).on_conflict_do_nothing()
        )
        budget = await session.scalar(
            select(RequestBudget).where(RequestBudget.key == key).with_for_update()
        )
        assert budget is not None
        start = max(now, budget.next_at)
        budget.next_at = start + timedelta(seconds=interval)
    delay = (start - utcnow()).total_seconds()
    if delay > 0:
        await asyncio.sleep(delay)


async def retry[T](action: Callable[[], Awaitable[T]], attempts: int) -> T:
    for attempt in range(attempts):
        try:
            return await action()
        except (
            RateLimited,
            TelegramRetryAfter,
            TelegramNetworkError,
            OSError,
            TimeoutError,
            TransientIntegrationError,
        ) as exc:
            if attempt + 1 == attempts:
                raise
            delay = max(2**attempt, getattr(exc, "retry_after", 0))
            # A large FloodWait belongs to a later cycle, not an occupied worker.
            if delay > 30:
                raise
            await asyncio.sleep(delay)
    raise AssertionError("Unreachable")


class BotRequestMiddleware(BaseRequestMiddleware):
    def __init__(self, database: Database, settings: Settings) -> None:
        self.database, self.settings = database, settings

    async def __call__(self, make_request, bot, method):  # type: ignore[no-untyped-def]
        async def send():  # type: ignore[no-untyped-def]
            await throttle(self.database, "bot-api", self.settings.bot_request_interval)
            target = getattr(method, "chat_id", None)
            if target is not None:
                async with self.database.transaction() as session:
                    known = await session.scalar(
                        select(User.id).where(User.telegram_user_id == target)
                    )
                if known:
                    await throttle(self.database, f"bot-chat:{target}", 1.05)
            return await make_request(bot, method)

        return await retry(send, self.settings.retry_attempts)
