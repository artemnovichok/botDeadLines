from typing import Protocol

from deadline_assistant.db.models import Event, TrackedChat
from deadline_assistant.domain import AnalysisContext, AnalysisOutput, MessageData
from deadline_assistant.services.digest import ConflictCard


class Analyzer(Protocol):
    model: str

    async def analyze(
        self,
        messages: list[MessageData],
        events: list[Event],
        timezone: str,
        context: AnalysisContext | None = None,
    ) -> AnalysisOutput: ...

    async def close(self) -> None: ...


class MessageReader(Protocol):
    async def fetch(self, encrypted_session: str, chat: TrackedChat) -> list[MessageData]: ...

    async def fetch_sources(
        self, encrypted_session: str, chat: TrackedChat, message_ids: list[int]
    ) -> dict[int, MessageData | None]: ...


class Notifier(Protocol):
    async def send(self, telegram_user_id: int, text: str) -> None: ...

    async def send_conflict(self, telegram_user_id: int, card: ConflictCard) -> None: ...
