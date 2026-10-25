from datetime import UTC, datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from deadline_assistant.config import Settings
from deadline_assistant.domain import EventData, MessageData, UserPreferences


@pytest.mark.parametrize(
    "data",
    [
        {"title": ""},
        {"title": "Дедлайн", "timezone": "Wrong/Zone"},
        {"title": "Дедлайн", "confidence": -0.1},
        {"title": "Дедлайн", "confidence": float("nan")},
        {"title": "Дедлайн", "confidence": 1.1},
        {"title": "Дедлайн", "user_id": "чужой"},
        {"title": "Дедлайн", "date_precision": "exact"},
        {"title": "Дедлайн", "due_at": "2026-10-20T10:00:00", "date_precision": "exact"},
        {"title": "Дедлайн", "due_at": "2026-10-20T10:00:00+03:00"},
    ],
)
def test_invalid_event(data: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        EventData.model_validate(data)


def test_event_normalizes_utc() -> None:
    data = EventData(
        title="  Курсовая  ",
        due_at=datetime(2026, 10, 20, 10, tzinfo=timezone(timedelta(hours=3))),
        date_precision="exact",
    )
    assert data.due_at == datetime(2026, 10, 20, 7, tzinfo=UTC)
    assert data.title == "Курсовая"


@pytest.mark.parametrize("value", ["08:00+03:00", "08:00:01", "25:00"])
def test_preferences_reject_invalid_wall_time(value: str) -> None:
    with pytest.raises(ValidationError):
        UserPreferences(timezone="Europe/Moscow", preferred_digest_time=value)


def test_message_requires_aware_date() -> None:
    with pytest.raises(ValidationError):
        MessageData(chat_id=-1001, message_id=1, text="Привет", timestamp=datetime(2026, 10, 20))


def test_message_preserves_original_text() -> None:
    text = "  Объявление\nСдать работу.\n "
    message = MessageData(chat_id=-1, message_id=1, text=text, timestamp=datetime.now(UTC))
    assert message.text == text


def test_configuration_errors_hide_secret_values() -> None:
    secret = "invalid-but-sensitive-encryption-key"
    with pytest.raises(ValidationError) as error:
        Settings(_env_file=None, session_encryption_key=secret)
    assert secret not in str(error.value)


def test_initial_history_default_and_legacy_configuration_are_capped(settings):
    assert settings.initial_message_limit == 100
    values = {**settings.model_dump(), "initial_message_limit": 1000}
    assert Settings(_env_file=None, **values).initial_message_limit == 100


@pytest.mark.parametrize(
    "overrides",
    [
        {"app_env": "production"},
        {"public_base_url": "http://example.org"},
        {"public_base_url": "https://example.org/path"},
        {"session_encryption_key": "invalid"},
        {"database_url": "sqlite:///sync.db"},
        {"sync_batch_size": 0},
        {"default_timezone": "Wrong/Zone"},
    ],
)
def test_settings_reject_unsafe_configuration(overrides: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        Settings(_env_file=None, **overrides)
