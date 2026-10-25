import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from tempfile import TemporaryDirectory
from uuid import UUID

from pydantic import SecretStr
from sqlalchemy import select

from deadline_assistant.config import Settings
from deadline_assistant.db.base import Base
from deadline_assistant.db.models import AnalysisRun, Conflict, Event, TelegramSession, User
from deadline_assistant.db.session import Database
from deadline_assistant.domain import EntityNotFound
from deadline_assistant.repositories.tenant import event_data
from deadline_assistant.services.delivery import SummaryDelivery
from deadline_assistant.services.interfaces import Notifier
from deadline_assistant.services.sync import SyncService


@dataclass
class CycleResult:
    telegram_user_id: int
    events: int = 0
    messages: int = 0
    conflicts: int = 0
    duration_seconds: float = 0
    usage: dict[str, object] = field(default_factory=dict)
    error: str | None = None
    status: str = "succeeded"
    proposals: list[dict[str, object]] = field(default_factory=list)


class CycleService:
    def __init__(
        self,
        database: Database,
        settings: Settings,
        sync: SyncService,
        notifier: Notifier | None,
    ) -> None:
        self.database, self.settings, self.sync = database, settings, sync
        self.summary = SummaryDelivery(database, notifier)

    async def users(self, telegram_user_id: int | None = None) -> list[User]:
        async with self.database.transaction() as session:
            query = select(User).where(User.enabled.is_(True))
            if telegram_user_id is not None:
                query = query.where(User.telegram_user_id == telegram_user_id)
            else:
                query = query.join(TelegramSession, TelegramSession.user_id == User.id).where(
                    TelegramSession.status == "connected",
                    User.study_group.is_not(None),
                    User.initial_analysis_choice.is_not(None),
                )
            users = list(await session.scalars(query.order_by(User.telegram_user_id)))
            if telegram_user_id is not None and not users:
                raise EntityNotFound("Пользователь не найден; сначала отправьте боту /start")
            return users

    async def run_user(self, user_id: UUID, *, send_now: bool = False) -> int:
        count = await self.sync.sync_user(user_id)
        if send_now:
            await self.summary.send_current(user_id)
        return count

    async def run_many(
        self,
        users: list[User],
        *,
        send_now: bool = False,
        dry_run: bool = False,
        on_result: Callable[[CycleResult], None] | None = None,
    ) -> list[CycleResult]:
        queue: asyncio.Queue[User] = asyncio.Queue()
        for user in users:
            queue.put_nowait(user)
        results: list[CycleResult] = []

        async def worker() -> None:
            while not queue.empty():
                user = queue.get_nowait()
                result = CycleResult(user.telegram_user_id)
                try:
                    if dry_run:
                        result = await self.preview(user)
                    else:
                        await self.run_user(user.id, send_now=send_now)
                        await self._metrics(self.database, user.id, result)
                except Exception as exc:
                    result.error = type(exc).__name__
                results.append(result)
                if on_result:
                    on_result(result)

        await asyncio.gather(
            *(worker() for _ in range(min(len(users), self.settings.cycle_concurrency)))
        )
        return results

    @staticmethod
    async def _metrics(database: Database, user_id: UUID, result: CycleResult) -> None:
        async with database.transaction() as session:
            run = await session.scalar(
                select(AnalysisRun)
                .where(AnalysisRun.user_id == user_id)
                .order_by(AnalysisRun.started_at.desc())
                .limit(1)
            )
            if run:
                result.status = run.status
                result.events, result.messages = run.event_count, run.new_message_count
                result.conflicts, result.duration_seconds = run.conflict_count, run.duration_seconds
                result.usage = run.token_usage

    async def preview(self, user: User) -> CycleResult:
        """Copy only this tenant, then execute the production pipeline in an isolated database."""
        from deadline_assistant.integrations.llm import OpenAIAnalyzer
        from deadline_assistant.integrations.telegram import TelegramGateway

        result = CycleResult(user.telegram_user_id)
        with TemporaryDirectory(prefix="deadline-preview-") as directory:
            settings = Settings(
                _env_file=None,
                **{
                    **self.settings.model_dump(),
                    "database_url": SecretStr(f"sqlite+aiosqlite:///{directory}/preview.db"),
                },
            )
            snapshot = Database(settings)
            owned_analyzer = None
            try:
                async with snapshot.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
                async with self.database.transaction() as source, snapshot.transaction() as dest:
                    for table in Base.metadata.sorted_tables:
                        if table.name == "users":
                            query = select(table).where(table.c.id == user.id)
                        elif "user_id" in table.c:
                            query = select(table).where(table.c.user_id == user.id)
                        else:
                            continue
                        rows = [dict(row) for row in (await source.execute(query)).mappings()]
                        if rows:
                            await dest.execute(table.insert(), rows)
                async with snapshot.transaction() as session:
                    before = {
                        str(e.id): event_data(e).model_dump(mode="json")
                        for e in await session.scalars(
                            select(Event).where(Event.user_id == user.id)
                        )
                    }
                    old_conflicts = set(await session.scalars(select(Conflict.id)))
                analyzer = self.sync.analyzer
                if isinstance(analyzer, OpenAIAnalyzer):
                    owned_analyzer = OpenAIAnalyzer(settings, snapshot)
                    owned_analyzer.semaphore = analyzer.semaphore
                    analyzer = owned_analyzer
                reader = self.sync.reader
                if isinstance(reader, TelegramGateway):
                    reader = TelegramGateway(settings, reader.cipher, snapshot)
                await SyncService(snapshot, reader, analyzer, settings).sync_user(user.id)
                await self._metrics(snapshot, user.id, result)
                async with snapshot.transaction() as session:
                    for event in await session.scalars(
                        select(Event).where(Event.user_id == user.id)
                    ):
                        data = event_data(event).model_dump(mode="json")
                        if before.get(str(event.id)) != data:
                            result.proposals.append(
                                {
                                    "действие": "изменить"
                                    if str(event.id) in before
                                    else "создать",
                                    "событие": data,
                                }
                            )
                    for conflict in await session.scalars(select(Conflict)):
                        if conflict.id not in old_conflicts:
                            result.proposals.append(
                                {"действие": "конфликт", "причина": conflict.reason}
                            )
            finally:
                if owned_analyzer:
                    await owned_analyzer.close()
                await snapshot.close()
        return result
