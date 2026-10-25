from collections.abc import Callable
from uuid import UUID

from deadline_assistant.config import Settings
from deadline_assistant.db.session import Database
from deadline_assistant.domain import StaleState
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.analysis import MemoryBuilder, OperationApplier
from deadline_assistant.services.batches import analyze_batch
from deadline_assistant.services.interfaces import Analyzer


class CorrectionService:
    def __init__(self, database: Database, analyzer: Analyzer, settings: Settings) -> None:
        self.database, self.analyzer, self.settings = database, analyzer, settings

    async def submit(
        self,
        user_id: UUID,
        event_id: UUID,
        text: str,
        chat_id: int | None = None,
        message_id: int | None = None,
        *,
        expected_version: int | None = None,
    ) -> str:
        # Persist first: a timeout leaves an unresolved correction for the next cycle.
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            repo = TenantRepository(session, user_id)
            if expected_version is not None:
                if (await repo.event(event_id)).version != expected_version:
                    raise StaleState("Событие изменилось. Выберите его в новом списке.")
            correction = await repo.correction(event_id, text, chat_id, message_id)
            correction_id = correction.id
            if correction.status != "pending":
                return correction.status
        return await self.process(user_id, correction_id)

    async def resume_pending(
        self, user_id: UUID, *, on_progress: Callable[[], None] | None = None
    ) -> None:
        from sqlalchemy import select

        from deadline_assistant.db.models import UserCorrection

        async with self.database.transaction() as session:
            ids = list(
                await session.scalars(
                    select(UserCorrection.id)
                    .where(UserCorrection.user_id == user_id, UserCorrection.status == "pending")
                    .order_by(UserCorrection.created_at)
                )
            )
        for correction_id in ids:
            await self.process(user_id, correction_id)
            if on_progress:
                on_progress()

    async def process(self, user_id: UUID, correction_id: UUID) -> str:
        async with self.database.lock(f"analysis:{user_id}", timeout=600):
            return await self._process(user_id, correction_id)

    async def _process(self, user_id: UUID, correction_id: UUID) -> str:
        for attempt in range(3):
            async with self.database.transaction() as session:
                user = await UserRepository(session).require(user_id)
                repo = TenantRepository(session, user_id)
                current = await repo.correction_by_id(correction_id)
                if current.status != "pending":
                    return current.status
                context = await MemoryBuilder(self.settings.llm_memory_event_limit).build(
                    repo, [], user.timezone, focus_correction_id=correction_id
                )
            output = await analyze_batch(
                self.database, self.analyzer, user_id, [], user.timezone, context
            )
            try:
                async with self.database.transaction() as session:
                    await UserRepository(session).require(user_id)
                    repo = TenantRepository(session, user_id)
                    correction = await repo.correction_by_id(correction_id)
                    if correction.status != "pending":
                        return correction.status
                    await OperationApplier().apply(repo, output.result, [], context)
                    return correction.status
            except StaleState:
                if attempt == 2:
                    raise
        raise AssertionError("Unreachable")

    async def recheck(self, user_id: UUID, conflict_id: UUID) -> None:
        async with self.database.lock(f"analysis:{user_id}", timeout=600):
            await self._recheck(user_id, conflict_id)

    async def _recheck(self, user_id: UUID, conflict_id: UUID) -> None:
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(user_id)
            repo = TenantRepository(session, user_id)
            await repo.conflict(conflict_id)
            context = await MemoryBuilder(self.settings.llm_memory_event_limit).build(
                repo, [], user.timezone, recheck_conflict_id=conflict_id
            )
        output = await analyze_batch(
            self.database, self.analyzer, user_id, [], user.timezone, context
        )
        async with self.database.transaction() as session:
            await UserRepository(session).require(user_id)
            await OperationApplier().apply(
                TenantRepository(session, user_id), output.result, [], context
            )
