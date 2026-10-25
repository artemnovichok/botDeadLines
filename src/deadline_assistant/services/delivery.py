from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from deadline_assistant.db.models import ScheduledDelivery
from deadline_assistant.db.session import Database
from deadline_assistant.domain import IntegrationUnavailable, utcnow
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.repositories.users import UserRepository
from deadline_assistant.services.digest import ConflictCard, build_digest
from deadline_assistant.services.interfaces import Notifier
from deadline_assistant.services.telegram_text import HtmlText


class SummaryDelivery:
    """One digest implementation for the scheduler and immediate CLI delivery."""

    def __init__(self, database: Database, notifier: Notifier | None) -> None:
        self.database, self.notifier = database, notifier

    async def send_current(self, user_id: UUID) -> None:
        if self.notifier is None:
            raise IntegrationUnavailable("Задайте TELEGRAM_BOT_TOKEN для отправки сводки")
        async with self.database.lock(f"summary:{user_id}", timeout=30):
            async with self.database.transaction() as session:
                await UserRepository(session).require(user_id)
                await TenantRepository(session, user_id).require_connection()
                delivery = ScheduledDelivery(
                    user_id=user_id, kind="manual:" + uuid4().hex[:22], local_date=utcnow().date()
                )
                session.add(delivery)
                await session.flush()
                delivery_id = delivery.id
            await self.deliver(user_id, delivery_id)
            async with self.database.transaction() as session:
                saved = await session.get(ScheduledDelivery, delivery_id)
                if saved:
                    saved.status, saved.completed_at = "sent", utcnow()

    async def deliver(
        self,
        user_id: UUID,
        delivery_id: UUID,
        *,
        stale_reason: str | None = None,
        now: datetime | None = None,
    ) -> None:
        from deadline_assistant.handlers.bot import telegram_chunks

        if self.notifier is None:
            raise IntegrationUnavailable("Отправка бота не настроена")
        async with self.database.transaction() as session:
            user = await UserRepository(session).require(user_id)
            await TenantRepository(session, user_id).require_connection()
            delivery = await session.get(ScheduledDelivery, delivery_id)
            if delivery is None or delivery.user_id != user_id:
                raise IntegrationUnavailable("Доставка отменена")
            if delivery.payload is None:
                cards, summary = await build_digest(
                    TenantRepository(session, user_id),
                    user.timezone,
                    stale_reason=stale_reason,
                    now=now,
                )
                payload: list[dict[str, Any]] = []
                for card in cards:
                    chunks = telegram_chunks(card.text)
                    for index, part in enumerate(chunks):
                        payload.append(
                            {
                                "kind": "conflict",
                                "text": part,
                                "buttons": card.buttons if index == len(chunks) - 1 else [],
                            }
                        )
                for index, part in enumerate(summary.chunks):
                    payload.append(
                        {
                            "kind": "html",
                            "text": part,
                            "list_id": str(summary.list_id)
                            if index == len(summary.chunks) - 1
                            else None,
                        }
                    )
                delivery.payload = payload
            payload, next_part = delivery.payload, delivery.next_part
        for index in range(next_part, len(payload)):
            # Recheck before every part, including cached payloads after a restart.
            async with self.database.transaction() as session:
                await UserRepository(session).require(user_id)
                await TenantRepository(session, user_id).require_connection()
            saved_part = payload[index]
            if saved_part["kind"] == "conflict":
                await self.notifier.send_conflict(
                    user.telegram_user_id,
                    ConflictCard(saved_part["text"], [(b[0], b[1]) for b in saved_part["buttons"]]),
                )
            else:
                text = HtmlText([saved_part["text"]])
                text.list_id = UUID(saved_part["list_id"]) if saved_part.get("list_id") else None
                await self.notifier.send(user.telegram_user_id, text)
            async with self.database.transaction() as session:
                delivery = await session.get(ScheduledDelivery, delivery_id)
                if delivery:
                    delivery.next_part = index + 1
