from datetime import time
from pathlib import Path
from typing import Literal
from urllib.parse import urlsplit
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from cryptography.fernet import Fernet
from pydantic import AliasChoices, Field, SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


def validate_timezone(value: str) -> str:
    try:
        ZoneInfo(value)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValueError("Неизвестный часовой пояс IANA") from exc
    return value


class DatabaseSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        extra="ignore",
        env_ignore_empty=True,
        hide_input_in_errors=True,
        populate_by_name=True,
    )
    database_url: SecretStr = SecretStr("sqlite+aiosqlite:///./data/deadline.db")


class Settings(DatabaseSettings):
    app_env: Literal["development", "production"] = "development"
    host: str = "127.0.0.1"
    forwarded_allow_ips: str = "127.0.0.1,::1"
    port: int = Field(default=8000, ge=1, le=65535)
    tls_cert_file: Path | None = None
    tls_key_file: Path | None = None
    public_base_url: str = "http://localhost:8000"
    session_encryption_key: SecretStr | None = None
    bot_token: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("TELEGRAM_BOT_TOKEN", "BOT_TOKEN")
    )
    telegram_api_id: int | None = None
    telegram_api_hash: SecretStr | None = None
    llm_api_key: SecretStr | None = Field(
        default=None, validation_alias=AliasChoices("LOWROUTER_API_KEY", "LLM_API_KEY")
    )
    llm_base_url: str = "https://api.lowrouter.ru/v1"
    llm_model: str = "gpt-6-luna"
    llm_json_mode: bool = True
    llm_structured_output: bool = True
    llm_stream: bool = False
    llm_timeout_seconds: float = Field(default=120, gt=0)
    llm_max_retries: int = Field(default=3, ge=0, le=10)
    llm_format_retries: int = Field(default=2, ge=0, le=5)
    llm_max_input_tokens: int = Field(default=30000, ge=2000)
    llm_max_output_tokens: int = Field(default=6000, ge=256)
    llm_token_encoding: str | None = None
    llm_reasoning_effort: Literal["none", "low", "medium", "high", "xhigh", "max"] | None = None
    llm_concurrency: int = Field(default=3, ge=1, le=20)
    cycle_concurrency: int = Field(default=4, ge=1, le=20)
    telegram_request_interval: float = Field(default=0.15, gt=0)
    bot_request_interval: float = Field(default=0.05, gt=0)
    retry_attempts: int = Field(default=3, ge=1, le=6)
    job_lock_timeout_seconds: float = Field(default=30, gt=0)
    llm_memory_event_limit: int = Field(default=200, ge=1, le=2000)
    default_timezone: str = "Europe/Moscow"
    default_digest_time: time = time(8)
    night_sync_time: time = time(2)
    daily_sync_hour: int | None = Field(default=None, ge=0, le=23)
    daily_digest_hour: int | None = Field(default=None, ge=0, le=23)
    digest_catchup_hours: int = Field(default=6, ge=1, le=24)
    delivery_max_attempts: int = Field(default=5, ge=1, le=20)
    initial_message_limit: int = 100
    message_max_age_days: int = Field(default=90, ge=1, le=3650)
    sync_batch_size: int = 50
    edit_lookback_limit: int = 100
    retention_days: int = 30
    sync_timeout_seconds: int = 600  # Maximum time without progress, not the full history duration.
    sync_max_duration_seconds: int = Field(default=300, ge=1)
    sync_max_total_tokens: int = Field(default=100000, ge=1)
    analysis_progress_interval_seconds: float = Field(default=120, gt=0)
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR"] = "INFO"

    _timezone = field_validator("default_timezone")(validate_timezone)

    @field_validator("default_digest_time", "night_sync_time")
    @classmethod
    def local_time(cls, value: time) -> time:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("Укажите местное время ЧЧ:ММ без секунд и смещения")
        return value

    @field_validator("session_encryption_key")
    @classmethod
    def encryption_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is not None:
            try:
                Fernet(value.get_secret_value().encode())
            except (ValueError, TypeError) as exc:
                raise ValueError("Нужен корректный ключ Fernet") from exc
        return value

    @model_validator(mode="after")
    def validate_settings(self) -> "Settings":
        if bool(self.tls_cert_file) != bool(self.tls_key_file):
            raise ValueError("TLS_CERT_FILE и TLS_KEY_FILE задаются вместе")
        if self.daily_sync_hour is not None:
            self.night_sync_time = time(self.daily_sync_hour)
        if self.daily_digest_hour is not None:
            self.default_digest_time = time(self.daily_digest_hour)
        llm = urlsplit(self.llm_base_url)
        if (
            llm.scheme not in {"http", "https"}
            or not llm.netloc
            or (llm.username or llm.password or llm.query or llm.fragment)
        ):
            raise ValueError("LLM_BASE_URL должен быть HTTP(S) адресом без встроенных секретов")
        if self.app_env == "production" and self.llm_api_key and llm.scheme != "https":
            raise ValueError("В production LLM_BASE_URL с API-ключом требует HTTPS")
        public = urlsplit(self.public_base_url)
        if public.scheme not in {"http", "https"} or not public.netloc:
            raise ValueError("PUBLIC_BASE_URL должен быть абсолютным HTTP(S) адресом")
        if (
            public.username
            or public.password
            or public.query
            or public.fragment
            or public.path not in {"", "/"}
        ):
            raise ValueError("PUBLIC_BASE_URL должен содержать только origin")
        if self.app_env == "production" and public.scheme != "https":
            raise ValueError("В production обязателен HTTPS")
        if self.bot_token and public.scheme != "https":
            raise ValueError("Для бота укажите PUBLIC_BASE_URL с HTTPS")
        if public.scheme == "http" and public.hostname not in {"localhost", "127.0.0.1", "::1"}:
            raise ValueError("HTTP допускается только для локального запуска")
        if not self.database_url.get_secret_value().startswith(
            ("sqlite+aiosqlite:", "postgresql+asyncpg:")
        ):
            raise ValueError("Нужен async URL SQLite или PostgreSQL")
        if self.telegram_api_id is not None and self.telegram_api_id <= 0:
            raise ValueError("TELEGRAM_API_ID должен быть положительным")
        if (
            min(
                self.initial_message_limit,
                self.sync_batch_size,
                self.edit_lookback_limit,
                self.retention_days,
                self.sync_timeout_seconds,
            )
            <= 0
        ):
            raise ValueError("Лимиты и сроки должны быть положительными")
        # Preserve existing .env files while enforcing the new history ceiling.
        self.initial_message_limit = min(self.initial_message_limit, 100)
        if self.sync_batch_size > 100:
            raise ValueError("SYNC_BATCH_SIZE не должен превышать 100")
        if self.bot_token and not self.session_encryption_key:
            raise ValueError("Для запуска бота задайте SESSION_ENCRYPTION_KEY")
        return self

    @property
    def telegram_enabled(self) -> bool:
        return bool(self.telegram_api_id and self.telegram_api_hash and self.session_encryption_key)

    @property
    def llm_enabled(self) -> bool:
        return bool(self.llm_api_key and self.llm_model)

    @property
    def static_dir(self) -> Path:
        return Path(__file__).parent / "api" / "static"
