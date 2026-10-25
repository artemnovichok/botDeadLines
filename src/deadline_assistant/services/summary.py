"""A deterministic summary, without an LLM call or fabricated dates/times."""

from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from html import escape
from urllib.parse import urlsplit
from uuid import UUID
from zoneinfo import ZoneInfo

from deadline_assistant.db.models import Event, SourceMessage
from deadline_assistant.domain import EventData, utcnow
from deadline_assistant.repositories.tenant import event_data
from deadline_assistant.services.lifecycle import boundary, calendar_range, expired
from deadline_assistant.services.telegram_text import HtmlText, escaped, units

MONTHS = (
    "",
    "января",
    "февраля",
    "марта",
    "апреля",
    "мая",
    "июня",
    "июля",
    "августа",
    "сентября",
    "октября",
    "ноября",
    "декабря",
)
SECTIONS = (
    "🔴 СЕГОДНЯ",
    "📅 НА ЭТОЙ НЕДЕЛЕ",
    "🗓 В ЭТОМ МЕСЯЦЕ",
    "📌 ОСТАЛЬНОЕ",
    "❔ НЕОПРЕДЕЛЁННОСТИ",
)


def russian_date(day: date, year: bool = False) -> str:
    return f"{day.day} {MONTHS[day.month]}" + (f" {day.year}" if year else "")


def safe_link(url: str | None) -> str | None:
    if not url or len(url) > 1000:
        return None
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme == "https"
            and parsed.hostname
            and not (parsed.username or parsed.password)
        ):
            encoded = escape(url, quote=True)
            return encoded if units(encoded) <= 700 else None
    except ValueError:
        pass
    return None


@dataclass
class SummaryItem:
    event: Event
    section: int
    sort_at: datetime
    late: bool = False


def group_events(
    events: list[Event],
    timezone: str,
    now: datetime | None = None,
    disputed: dict[UUID, list[EventData]] | None = None,
) -> list[list[SummaryItem]]:
    now = now or utcnow()
    zone = ZoneInfo(timezone)
    today = now.astimezone(zone).date()
    sunday = today + timedelta(days=6 - today.weekday())
    groups: list[list[SummaryItem]] = [[] for _ in SECTIONS]
    for event in events:
        recurring = event.recurring_deadline_id is not None
        if event.status != "active" and not (recurring and event.status == "completed"):
            continue
        if recurring and event.due_at is not None and event.due_at < now:
            continue
        data = event_data(event)
        variants = (disputed or {}).get(event.id, [])
        unknown = data.due_at is None
        if expired(data, timezone, now) and not variants:
            # Today's events remain in the calendar even if their known hour has passed.
            if data.due_at is None or data.due_at.astimezone(zone).date() != today:
                continue
        # An unresolved conflict stays visible even if its stored date has already passed.
        candidates = [
            d.due_at
            for d in [data, *variants]
            if d.due_at is not None and not expired(d, timezone, now)
        ]
        point = min(candidates) if candidates else data.due_at
        if (
            data.due_at
            and data.end_at
            and data.due_at <= now < boundary(data.end_at, data.date_precision, timezone)
        ):
            point = now  # A multi-day event already in progress is relevant today.
        late = False
        if data.due_at and now >= boundary(
            data.end_at or data.due_at, data.date_precision, timezone
        ):
            if data.resubmission_start or data.resubmission_end:
                start = data.resubmission_start
                point = start if start and start.astimezone(zone).date() > today else now
                late = True
        day = point.astimezone(zone).date() if point else None
        if (
            unknown
            or data.date_precision == "approximate"
            or data.needs_clarification
            or variants
            or day is None
            or day < today
        ):
            section = 4
        elif day == today:
            section = 0
        elif day <= sunday:
            section = 1
        elif day.year == today.year and day.month == today.month:
            section = 2
        else:
            section = 3
        groups[section].append(
            SummaryItem(event, section, point or datetime.max.replace(tzinfo=UTC), late)
        )
    for group in groups:
        group.sort(key=lambda item: (item.sort_at, str(item.event.id)))
    return groups


def summary_event(
    item: SummaryItem,
    number: int,
    timezone: str,
    sources: list[SourceMessage],
    variants: list[EventData],
) -> str:
    data = event_data(item.event)
    zone = ZoneInfo(timezone)
    title = data.title
    if data.subject and data.subject.casefold() not in title.casefold():
        title = data.subject + " — " + title
    elif data.subject is None and data.event_type in {"test", "exam", "credit"}:
        chat_titles = {source.chat_title for source in sources if source.chat_title}
        if len(chat_titles) == 1:
            chat_title = next(iter(chat_titles))
            if chat_title.casefold() not in title.casefold():
                title = chat_title + " — " + title
    title_html = escaped(" ".join(title.split()), 220)
    closed = item.event.recurring_deadline_id is not None and data.status == "completed"
    if closed:
        title_html = f"<s>{title_html}</s>"
    lines = [f"{number}. <b>{title_html}</b>"]
    if data.due_at:
        local = data.due_at.astimezone(zone)
        label = "Основной срок: " if item.late else ""
        when = russian_date(local.date(), True)
        day_range = calendar_range(data, timezone)
        if not day_range:
            when += f", {local:%H:%M}"
            if data.end_at:
                end = data.end_at.astimezone(zone)
                when += "–" + (
                    f"{end:%H:%M}"
                    if end.date() == local.date()
                    else russian_date(end.date(), True) + f", {end:%H:%M}"
                )
            if data.date_precision == "approximate":
                when = "Примерно " + when
            elif data.schedule_kind == "deadline" and not data.end_at:
                when = "До " + when
        elif data.end_at:
            end_day = data.end_at.astimezone(zone).date()
            if end_day != local.date():
                when += " — " + russian_date(end_day, True)
        if day_range and data.date_precision == "approximate":
            when = "Примерно " + when
        lines.append(label + when)
    else:
        lines.append("Дата уточняется")
    if variants:
        dates = []
        for variant in variants:
            when = "дата уточняется"
            if variant.due_at:
                point = variant.due_at.astimezone(zone)
                when = russian_date(point.date(), True)
                variant_days = calendar_range(variant, timezone)
                if not variant_days:
                    when += f", {point:%H:%M}"
                if variant.end_at:
                    end = variant.end_at.astimezone(zone)
                    if not variant_days or end.date() != point.date():
                        when += " — " + russian_date(end.date(), True)
                        if not variant_days:
                            when += f", {end:%H:%M}"
                if variant.date_precision == "approximate":
                    when = "Примерно " + when
            if when not in dates:
                dates.append(when)
        if len(dates) == 1:
            lines[1] = escape(dates[0]) + " · условия требуют подтверждения"
        else:
            lines[1] = "Дата требует подтверждения: " + escaped(" / ".join(dates), 240)
    elif item.section == 4 and data.description:
        lines.append(escaped(" ".join(data.description.split()), 120))
    elif data.needs_clarification and data.date_precision != "approximate":
        lines[1] += " · требуется уточнение"
    if closed:
        lines[1] = f"<s>{lines[1]}</s>"
    details = []
    if item.event.recurring_deadline_id is not None:
        details.append("🔁 Циклический дедлайн" + (" · закрыт" if closed else ""))
    if data.schedule_kind == "expected_release":
        details.append("Ожидается выдача задания")
    if data.is_optional:
        details.append("Необязательно")
    if item.late:
        pending = data.resubmission_start is not None and item.sort_at == data.resubmission_start
        details.append("Предстоит досдача" if pending else "Действует период досдачи")
    if data.resubmission_start or data.resubmission_end:
        dates = []
        for late_point in (data.resubmission_start, data.resubmission_end):
            if late_point is None:
                dates.append("уточняется")
            else:
                local = late_point.astimezone(zone)
                dates.append(
                    russian_date(local.date(), True)
                    + (f", {local:%H:%M}" if data.resubmission_precision != "day" else "")
                )
        details.append("Досдача: " + " — ".join(dates))
    for label, text in (
        ("", data.submission_conditions),
        ("Штраф: ", data.penalties),
        ("Место: ", data.location or ""),
    ):
        if text and item.section != 4:
            details.append(label + escaped(" ".join(text.split()), 140))
    if details:
        lines.append(" · ".join(details))
    link = next(
        (
            safe_link(s.source_url)
            for s in sorted(
                sources,
                key=lambda s: (s.available, s.edit_timestamp or s.message_timestamp),
                reverse=True,
            )
            if safe_link(s.source_url)
        ),
        None,
    )
    if link:
        lines[1] += f' · <a href="{link}">ссылка</a>'
    elif sources:
        lines[1] += " · ссылка недоступна"
    return "\n".join(lines)


def render_summary(
    groups: list[list[SummaryItem]],
    timezone: str,
    sources: dict[UUID, list[SourceMessage]],
    disputed: dict[UUID, list[EventData]],
    *,
    now: datetime | None = None,
    stale: str | None = None,
) -> HtmlText:
    now = now or utcnow()
    blocks = [
        "📚 <b>УЧЕБНЫЕ ДЕДЛАЙНЫ</b>\nАктуально на "
        + russian_date(now.astimezone(ZoneInfo(timezone)).date(), True)
    ]
    if stale:
        blocks.append("⚠️ " + escape(stale))
    number = 0
    for index, group in enumerate(groups):
        if not group:
            continue
        header = SECTIONS[index]
        for offset, item in enumerate(group):
            number += 1
            block = summary_event(
                item,
                number,
                timezone,
                sources.get(item.event.id, []),
                disputed.get(item.event.id, []),
            )
            blocks.append((header + "\n\n" if offset == 0 else "") + block)
    if number == 0:
        blocks.append("Активных учебных событий пока нет.")
    blocks[0] += " · " + escape(timezone)
    return HtmlText(blocks)
