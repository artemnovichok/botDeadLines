from dataclasses import dataclass
from datetime import datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from sqlalchemy import select

from deadline_assistant.db.models import (
    AnalysisRun,
    ConflictOptionSource,
    Event,
    SourceMessage,
    UserCorrection,
)
from deadline_assistant.domain import EventData, utcnow
from deadline_assistant.repositories.tenant import TenantRepository
from deadline_assistant.services.lifecycle import calendar_range
from deadline_assistant.services.summary import group_events, render_summary
from deadline_assistant.services.telegram_text import HtmlText


def render_date(data: EventData, timezone: str) -> str:
    if data.due_at is None:
        return "дата уточняется"
    zone = ZoneInfo(timezone)
    local = data.due_at.astimezone(zone)
    day_range = calendar_range(data, timezone)
    date = local.strftime("%d.%m.%Y" if day_range else "%d.%m.%Y %H:%M")
    if data.end_at:
        end = data.end_at.astimezone(zone)
        if not day_range or end.date() != local.date():
            date += " — " + end.strftime("%d.%m.%Y" if day_range else "%d.%m.%Y %H:%M")
    if data.date_precision == "approximate":
        date += " (приблизительно)"
    if data.schedule_kind == "expected_release":
        date += " (ожидается выдача)"
    if data.status != "active":
        date += " · " + ("отменено" if data.status == "cancelled" else "завершено")
    return date


def render_details(data: EventData, timezone: str) -> list[str]:
    lines = []
    if data.subject:
        lines.append("Предмет: " + data.subject)
    if data.is_optional:
        lines.append("Необязательно")
    if data.submission_conditions:
        lines.append("Условия: " + data.submission_conditions)
    if data.penalties:
        lines.append("Штрафы: " + data.penalties)
    if data.resubmission_start or data.resubmission_end:
        dates = [
            d.astimezone(ZoneInfo(timezone)).strftime(
                "%d.%m.%Y" if data.resubmission_precision == "day" else "%d.%m.%Y %H:%M"
            )
            if d
            else "уточняется"
            for d in (data.resubmission_start, data.resubmission_end)
        ]
        lines.append("Досдача: " + " — ".join(dates))
    if data.location:
        lines.append("Место: " + data.location)
    return lines


def render_events(
    events: list[Event], timezone: str, disputed: dict[UUID, list[EventData]] | None = None
) -> str:
    if not events:
        return "Активных учебных событий пока нет."
    lines = ["Ваши учебные события:"]
    for event in events:
        data = EventData.model_validate({k: getattr(event, k) for k in EventData.model_fields})
        variants = (disputed or {}).get(event.id)
        if variants:
            date = "Требует уточнения: " + "; ".join(
                dict.fromkeys(render_date(v, timezone) for v in variants)
            )
        else:
            date = render_date(data, timezone)
            if data.needs_clarification:
                date += " · Требует уточнения"
        lines.append(f"• {event.title} — {date}")
        lines.extend("  " + detail for detail in render_details(data, timezone))
        lines.append(f"  ID: {event.id}")
    lines.append(f"Часовой пояс: {timezone}.")
    return "\n".join(lines)


@dataclass
class ConflictCard:
    text: str
    buttons: list[tuple[str, str]]


async def build_digest(
    repo: TenantRepository,
    timezone: str,
    *,
    now: datetime | None = None,
    stale_reason: str | None = None,
) -> tuple[list[ConflictCard], HtmlText]:
    now = now or utcnow()
    cards = []
    disputed: dict[UUID, list[EventData]] = {}
    for conflict in await repo.conflicts():
        event = await repo.event(conflict.event_id)
        options = await repo.conflict_options(conflict.id)
        variants = [EventData.model_validate(o.proposed_event_data) for o in options]
        if event.status != "active":
            continue
        disputed[event.id] = variants
        lines = [f"Обнаружен конфликт по событию «{event.title}»."]
        buttons = []
        for option, data in zip(options, variants, strict=True):
            lines.append(f"\nВариант {option.option_number}: {render_date(data, timezone)}")
            lines.append(data.title)
            lines.append(data.description)
            lines.extend(render_details(data, timezone))
            sources = list(
                await repo.session.scalars(
                    select(SourceMessage)
                    .join(ConflictOptionSource, ConflictOptionSource.source_id == SourceMessage.id)
                    .where(
                        ConflictOptionSource.option_id == option.id,
                        ConflictOptionSource.user_id == repo.user_id,
                        SourceMessage.user_id == repo.user_id,
                    )
                )
            )
            for source in sources:
                link = (
                    source.source_url
                    or f"чат {source.source_chat_id}, сообщение {source.source_message_id}"
                )
                lines.append(
                    f"Источник: {link}" + (" (недоступен)" if not source.available else "")
                )
                author = source.author_metadata.get("name") or source.author_metadata.get("id")
                lines.append(
                    f"Автор: {author or 'неизвестен'}; "
                    + source.message_timestamp.astimezone(ZoneInfo(timezone)).strftime(
                        "%d.%m.%Y %H:%M"
                    )
                )
                if source.edit_timestamp:
                    lines.append(
                        "Изменён: "
                        + source.edit_timestamp.astimezone(ZoneInfo(timezone)).strftime(
                            "%d.%m.%Y %H:%M"
                        )
                    )
            for correction_id in option.correction_ids:
                correction = await repo.correction_by_id(UUID(correction_id))
                lines.append(
                    (
                        "Отменённое исправление"
                        if correction.status == "reverted"
                        else "Исправление пользователя"
                    )
                    + f" от {correction.created_at.isoformat()}: "
                    + correction.original_text[:400]
                )
            buttons.append(
                (
                    f"Вариант {option.option_number}",
                    f"cf:{conflict.id.hex}:{conflict.event_version}:{option.option_number}",
                )
            )
        lines.append("\nКакой вариант считать правильным?")
        buttons.append(
            ("Оставить неподтверждённым", f"cf:{conflict.id.hex}:{conflict.event_version}:0")
        )
        if conflict.event_version != event.version:
            lines.append("Событие изменилось после подготовки вариантов. Проверьте конфликт снова.")
            buttons = [("Проверить снова", f"cfcheck:{conflict.id.hex}")]
        from deadline_assistant.handlers.event_actions import short_id

        buttons.append(("Посмотреть источники", f"evsrc:{short_id(event.id)}"))
        cards.append(ConflictCard("\n".join(lines), buttons))
    groups = group_events(await repo.active_events(), timezone, now, disputed)
    items = [item.event for group in groups for item in group]
    sources_by_event = {e.id: await repo.current_sources(e.id) for e in items}
    latest = await repo.session.scalar(
        select(AnalysisRun)
        .where(AnalysisRun.user_id == repo.user_id)
        .order_by(AnalysisRun.started_at.desc())
        .limit(1)
    )
    stale = stale_reason
    if latest and latest.status == "partial":
        stale = "Обработана часть истории. События сохранены; продолжить анализ: /sync."
    if latest and latest.status in {"failed", "interrupted"}:
        stale = (
            "Данные могут быть устаревшими: последний анализ не завершён. "
            "Показаны сохранённые события."
        )
    if await repo.session.scalar(
        select(UserCorrection.id)
        .where(UserCorrection.user_id == repo.user_id, UserCorrection.status == "pending")
        .limit(1)
    ):
        stale = "Есть ещё не обработанное исправление. Показаны последние сохранённые данные."
    summary = render_summary(groups, timezone, sources_by_event, disputed, now=now, stale=stale)
    listing = await repo.save_event_list(items)
    summary.list_id = listing.id
    return cards, summary
