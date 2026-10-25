from datetime import date, datetime, time
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    BigInteger,
    CheckConstraint,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from deadline_assistant.db.base import Base, UTCDateTime
from deadline_assistant.domain import utcnow


class Identity:
    id: Mapped[UUID] = mapped_column(primary_key=True, default=uuid4)


class Owned:
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)


class User(Identity, Base):
    __tablename__ = "users"
    telegram_user_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    study_group: Mapped[str | None] = mapped_column(String(32))
    study_group_prompted_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    timezone: Mapped[str] = mapped_column(String(100), default="Europe/Moscow")
    preferred_digest_time: Mapped[time] = mapped_column(default=time(8))
    enabled: Mapped[bool] = mapped_column(default=True)
    bot_blocked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)


class TelegramSession(Identity, Owned, Base):
    __tablename__ = "telegram_sessions"
    __table_args__ = (UniqueConstraint("user_id"),)
    encrypted_session_data: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="connected")
    connected_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    last_error: Mapped[str | None] = mapped_column(String(100))


class TrackedChat(Identity, Owned, Base):
    __tablename__ = "tracked_chats"
    __table_args__ = (UniqueConstraint("user_id", "telegram_chat_id"),)
    telegram_chat_id: Mapped[int] = mapped_column(BigInteger)
    peer_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    title: Mapped[str] = mapped_column(String(300))
    enabled: Mapped[bool] = mapped_column(default=True)
    last_processed_message_id: Mapped[int] = mapped_column(BigInteger, default=0)
    last_successful_sync_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    initialization_status: Mapped[str] = mapped_column(String(30), default="pending")
    initial_upper_message_id: Mapped[int | None] = mapped_column(BigInteger)


class Event(Identity, Owned, Base):
    __tablename__ = "events"
    __table_args__ = (
        UniqueConstraint("id", "user_id"),
        CheckConstraint("confidence >= 0 AND confidence <= 1", name="confidence_range"),
        CheckConstraint("status IN ('active','completed','cancelled')", name="valid_status"),
        Index("ix_events_user_status_due", "user_id", "status", "due_at"),
    )
    title: Mapped[str] = mapped_column(String(300))
    subject: Mapped[str | None] = mapped_column(String(200))
    description: Mapped[str] = mapped_column(Text, default="")
    event_type: Mapped[str] = mapped_column(String(30), default="deadline")
    due_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    timezone: Mapped[str] = mapped_column(String(100))
    date_precision: Mapped[str] = mapped_column(String(30), default="unknown")
    status: Mapped[str] = mapped_column(String(30), default="active")
    is_optional: Mapped[bool] = mapped_column(default=False)
    location: Mapped[str | None] = mapped_column(String(500))
    confidence: Mapped[float] = mapped_column(default=0)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, onupdate=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_by_user: Mapped[bool] = mapped_column(default=False, server_default="0")
    retain_until: Mapped[datetime | None] = mapped_column(UTCDateTime())
    version: Mapped[int] = mapped_column(default=1, server_default="1")
    end_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    schedule_kind: Mapped[str] = mapped_column(
        String(30), default="deadline", server_default="deadline"
    )
    needs_clarification: Mapped[bool] = mapped_column(default=False, server_default="0")
    submission_conditions: Mapped[str] = mapped_column(Text, default="", server_default="")
    penalties: Mapped[str] = mapped_column(Text, default="", server_default="")
    resubmission_start: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resubmission_end: Mapped[datetime | None] = mapped_column(UTCDateTime())
    resubmission_precision: Mapped[str] = mapped_column(
        String(30), default="day", server_default="day"
    )
    dedup_key: Mapped[str | None] = mapped_column(String(64), index=True)
    __mapper_args__ = {"version_id_col": version}


class SourceMessage(Identity, Owned, Base):
    """Одна версия исходного сообщения может подтверждать несколько событий."""

    __tablename__ = "source_messages"
    __table_args__ = (
        UniqueConstraint("id", "user_id"),
        UniqueConstraint("user_id", "source_chat_id", "source_message_id", "revision"),
    )
    source_message_id: Mapped[int] = mapped_column(BigInteger)
    source_chat_id: Mapped[int] = mapped_column(BigInteger)
    revision: Mapped[str] = mapped_column(String(64))
    original_text: Mapped[str | None] = mapped_column(Text)
    message_timestamp: Mapped[datetime] = mapped_column(UTCDateTime())
    edit_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime())
    author_metadata: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    source_url: Mapped[str | None] = mapped_column(Text)
    chat_title: Mapped[str] = mapped_column(String(300), default="", server_default="")
    reply_to_message_id: Mapped[int | None] = mapped_column(BigInteger)
    available: Mapped[bool] = mapped_column(default=True, server_default="1")
    checked_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class EventSource(Identity, Base):
    __tablename__ = "event_sources"
    __table_args__ = (
        UniqueConstraint("event_id", "source_id"),
        ForeignKeyConstraint(
            ["event_id", "user_id"], ["events.id", "events.user_id"], ondelete="CASCADE"
        ),
        ForeignKeyConstraint(
            ["source_id", "user_id"],
            ["source_messages.id", "source_messages.user_id"],
            ondelete="CASCADE",
        ),
    )
    user_id: Mapped[UUID] = mapped_column(index=True)
    event_id: Mapped[UUID] = mapped_column(index=True)
    source_id: Mapped[UUID] = mapped_column(index=True)
    source_role: Mapped[str] = mapped_column(String(30), default="confirmation")


class UserCorrection(Identity, Owned, Base):
    __tablename__ = "user_corrections"
    __table_args__ = (
        ForeignKeyConstraint(
            ["event_id", "user_id"], ["events.id", "events.user_id"], ondelete="CASCADE"
        ),
    )
    event_id: Mapped[UUID] = mapped_column(index=True)
    original_text: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    status: Mapped[str] = mapped_column(String(30), default="pending")
    applied_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    telegram_chat_id: Mapped[int | None] = mapped_column(BigInteger)
    telegram_message_id: Mapped[int | None] = mapped_column(BigInteger)
    previous_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    applied_data: Mapped[dict[str, Any] | None] = mapped_column(JSON)


class Conflict(Identity, Owned, Base):
    __tablename__ = "conflicts"
    __table_args__ = (
        UniqueConstraint("id", "user_id"),
        ForeignKeyConstraint(
            ["event_id", "user_id"], ["events.id", "events.user_id"], ondelete="CASCADE"
        ),
        Index("ix_conflicts_user_status", "user_id", "status"),
    )
    event_id: Mapped[UUID] = mapped_column(index=True)
    conflict_type: Mapped[str] = mapped_column(String(50), default="event_change")
    event_version: Mapped[int] = mapped_column(default=1, server_default="1")
    reason: Mapped[str] = mapped_column(Text, default="", server_default="")
    status: Mapped[str] = mapped_column(String(30), default="open")
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())


class ConflictOption(Identity, Base):
    __tablename__ = "conflict_options"
    __table_args__ = (
        UniqueConstraint("conflict_id", "option_number"),
        UniqueConstraint("id", "user_id"),
        ForeignKeyConstraint(
            ["conflict_id", "user_id"], ["conflicts.id", "conflicts.user_id"], ondelete="CASCADE"
        ),
    )
    user_id: Mapped[UUID] = mapped_column(index=True)
    conflict_id: Mapped[UUID] = mapped_column(index=True)
    option_number: Mapped[int]
    proposed_event_data: Mapped[dict[str, Any]] = mapped_column(JSON)
    correction_ids: Mapped[list[str]] = mapped_column(JSON, default=list, server_default="[]")
    label: Mapped[str] = mapped_column(String(300), default="", server_default="")


class ConflictOptionSource(Base):
    __tablename__ = "conflict_option_sources"
    __table_args__ = (
        ForeignKeyConstraint(
            ["option_id", "user_id"],
            ["conflict_options.id", "conflict_options.user_id"],
            ondelete="CASCADE",
        ),
        ForeignKeyConstraint(
            ["source_id", "user_id"],
            ["source_messages.id", "source_messages.user_id"],
            ondelete="CASCADE",
        ),
    )
    option_id: Mapped[UUID] = mapped_column(primary_key=True)
    source_id: Mapped[UUID] = mapped_column(primary_key=True)
    user_id: Mapped[UUID] = mapped_column(index=True)


class AnalysisRun(Identity, Owned, Base):
    __tablename__ = "analysis_runs"
    started_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    status: Mapped[str] = mapped_column(String(30), default="running")
    model: Mapped[str] = mapped_column(String(200))
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(30), default="sync", server_default="sync")
    chat_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    new_message_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    event_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    conflict_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    error_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")
    duration_seconds: Mapped[float] = mapped_column(Float, default=0, server_default="0")


class AnalysisBatch(Identity, Owned, Base):
    """Durable validated LLM result; no ordinary chat texts are stored here."""

    __tablename__ = "analysis_batches"
    __table_args__ = (UniqueConstraint("user_id", "fingerprint"),)
    fingerprint: Mapped[str] = mapped_column(String(64))
    output: Mapped[dict[str, Any]] = mapped_column(JSON)
    token_usage: Mapped[dict[str, Any]] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class RequestBudget(Base):
    __tablename__ = "request_budgets"
    key: Mapped[str] = mapped_column(String(100), primary_key=True)
    next_at: Mapped[datetime] = mapped_column(UTCDateTime())


class ProcessedMessage(Owned, Base):
    __tablename__ = "processed_messages"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    chat_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    message_id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    edit_timestamp: Mapped[datetime | None] = mapped_column(UTCDateTime())
    revision: Mapped[str] = mapped_column(String(64))
    processed_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class AuthLink(Identity, Owned, Base):
    __tablename__ = "auth_links"
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    browser_hash: Mapped[str | None] = mapped_column(String(64), unique=True)
    csrf_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)
    consumed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())

    approved_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, server_default="0")


class ScheduledDelivery(Identity, Owned, Base):
    __tablename__ = "scheduled_deliveries"
    __table_args__ = (UniqueConstraint("user_id", "kind", "local_date"),)
    kind: Mapped[str] = mapped_column(String(30))
    local_date: Mapped[date]
    status: Mapped[str] = mapped_column(String(30), default="pending")
    attempts: Mapped[int] = mapped_column(default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(UTCDateTime())
    payload: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON)
    next_part: Mapped[int] = mapped_column(default=0, server_default="0")


class EventList(Identity, Owned, Base):
    """Immutable numbering for one displayed list, including event versions."""

    __tablename__ = "event_lists"
    entries: Mapped[list[dict[str, Any]]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(UTCDateTime(), index=True)


class CallbackReceipt(Owned, Base):
    __tablename__ = "callback_receipts"
    key: Mapped[str] = mapped_column(String(64), primary_key=True)
    created_at: Mapped[datetime] = mapped_column(UTCDateTime(), default=utcnow, index=True)


class EventTombstone(Identity, Owned, Base):
    """Minimal identity prevents deleted completed work from being recreated by analysis."""

    __tablename__ = "event_tombstones"
    __table_args__ = (UniqueConstraint("user_id", "dedup_key"),)
    dedup_key: Mapped[str] = mapped_column(String(64))
