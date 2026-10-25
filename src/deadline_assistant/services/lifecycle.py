"""Calendar boundaries used for visibility and retention, never advertised as submission times."""

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo

from deadline_assistant.domain import EventData


def calendar_range(data: EventData, timezone: str) -> bool:
    """A model's approximate whole-day interval must not advertise invented hours."""
    if data.date_precision == "day":
        return True
    if data.date_precision != "approximate" or not data.due_at or not data.end_at:
        return False
    zone = ZoneInfo(timezone)
    start, end = data.due_at.astimezone(zone), data.end_at.astimezone(zone)
    return start.time() == time() and (end.hour, end.minute) == (23, 59)


def boundary(value: datetime, precision: str, timezone: str) -> datetime:
    if precision == "day":
        day = value.astimezone(ZoneInfo(timezone)).date() + timedelta(days=1)
        return datetime.combine(day, time(), ZoneInfo(timezone)).astimezone(UTC)
    return value


def lifecycle_end(data: EventData, timezone: str) -> datetime | None:
    if data.due_at is None:
        return None
    ends = [boundary(data.end_at or data.due_at, data.date_precision, timezone)]
    if data.resubmission_start and data.resubmission_end is None:
        return None  # A known start is not evidence that late submission has ended.
    if data.resubmission_end:
        ends.append(boundary(data.resubmission_end, data.resubmission_precision, timezone))
    return max(ends)


def expired(data: EventData, timezone: str, now: datetime) -> bool:
    end = lifecycle_end(data, timezone)
    return end is not None and now >= end
