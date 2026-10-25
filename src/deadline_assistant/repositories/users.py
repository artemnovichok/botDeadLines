from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from deadline_assistant.config import Settings
from deadline_assistant.db.models import TelegramSession, TrackedChat, User
from deadline_assistant.domain import (
    EntityNotFound,
    IntegrationUnavailable,
    UserPreferences,
    normalize_study_group,
    utcnow,
)


class UserRepository:
    def __init__(self, session: AsyncSession) -> None:
        self.session = session

    async def by_telegram_id(self, telegram_id: int) -> User | None:
        return await self.session.scalar(select(User).where(User.telegram_user_id == telegram_id))

    async def get_or_create(self, telegram_id: int, settings: Settings) -> User:
        if telegram_id <= 0:
            raise ValueError("Нужен положительный Telegram user_id")
        existing = await self.by_telegram_id(telegram_id)
        if existing:
            return existing
        try:
            async with self.session.begin_nested():
                user = User(
                    telegram_user_id=telegram_id,
                    timezone=settings.default_timezone,
                    preferred_digest_time=settings.default_digest_time,
                )
                self.session.add(user)
                await self.session.flush()
            return user
        except IntegrityError:
            existing = await self.by_telegram_id(telegram_id)
            if existing is None:
                raise
            return existing

    async def require(self, user_id: UUID) -> User:
        # На PostgreSQL сериализует короткие изменения аккаунта, входа и порций анализа.
        # Сетевые вызовы выполняются после закрытия этой транзакции.
        user = await self.session.scalar(select(User).where(User.id == user_id).with_for_update())
        if user is None or not user.enabled:
            raise EntityNotFound
        return user

    async def preferences(self, user_id: UUID, preferences: UserPreferences) -> User:
        user = await self.require(user_id)
        user.timezone = preferences.timezone
        user.preferred_digest_time = preferences.preferred_digest_time
        return user

    async def set_study_group(self, user_id: UUID, value: str) -> User:
        group = normalize_study_group(value)
        user = await self.require(user_id)
        user.study_group = group
        user.study_group_prompted_at = user.study_group_prompted_at or utcnow()
        return user

    async def enabled(self) -> list[User]:
        """Системный обход для планировщика; не используется в пользовательском API."""
        return list(await self.session.scalars(select(User).where(User.enabled.is_(True))))

    async def decide_initial_analysis(self, user_id: UUID, *, analyze: bool) -> bool:
        user = await self.require(user_id)
        if user.initial_analysis_choice is not None:
            return False
        credential = await self.session.scalar(
            select(TelegramSession).where(TelegramSession.user_id == user_id)
        )
        if credential is None or credential.status != "connected" or not user.study_group:
            raise IntegrationUnavailable("Сначала подключите Telegram и укажите учебную группу")
        chats = list(
            await self.session.scalars(
                select(TrackedChat).where(
                    TrackedChat.user_id == user_id, TrackedChat.enabled.is_(True)
                )
            )
        )
        if not chats:
            raise IntegrationUnavailable("Сначала выберите чаты в «Мои чаты»")
        user.initial_analysis_choice = analyze
        user.initial_analysis_decided_at = utcnow().replace(microsecond=0)
        if not analyze:
            for chat in chats:
                chat.history_after = user.initial_analysis_decided_at
                chat.initialization_status = "ready"
        return True
