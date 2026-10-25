import re
from datetime import UTC, datetime, time
from enum import StrEnum
from typing import Annotated, Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from deadline_assistant.config import validate_timezone


def utcnow() -> datetime:
    return datetime.now(UTC)


def normalize_study_group(value: str) -> str:
    compact = re.sub(r"[\s-]+", "", value).upper()
    if not re.fullmatch(r"[A-ZА-ЯЁ]{2,12}[0-9]{3,6}", compact):
        raise ValueError("Введите учебную группу с буквами и номером, например БПИ267.")
    return compact


class EventStatus(StrEnum):
    ACTIVE = "active"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class EventType(StrEnum):
    DEADLINE = "deadline"
    HOMEWORK = "homework"
    LAB = "lab"
    TEST = "test"
    EXAM = "exam"
    CREDIT = "credit"
    CONSULTATION = "consultation"
    MEETING = "meeting"
    EDUCATIONAL = "educational"
    REGISTRATION = "registration"
    RESUBMISSION = "resubmission"
    OTHER = "other"


class DatePrecision(StrEnum):
    EXACT = "exact"
    DAY = "day"
    APPROXIMATE = "approximate"
    UNKNOWN = "unknown"


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class UserPreferences(StrictModel):
    timezone: str
    preferred_digest_time: time
    _timezone = field_validator("timezone")(validate_timezone)

    @field_validator("preferred_digest_time")
    @classmethod
    def local_time(cls, value: time) -> time:
        if value.tzinfo is not None or value.second or value.microsecond:
            raise ValueError("Нужно местное время ЧЧ:ММ")
        return value


class EventData(StrictModel):
    title: str = Field(min_length=1, max_length=300)
    subject: str | None = Field(default=None, max_length=200)
    description: str = Field(default="", max_length=6000)
    event_type: EventType = EventType.DEADLINE
    due_at: datetime | None = None
    end_at: datetime | None = None
    timezone: str = "Europe/Moscow"
    date_precision: DatePrecision = DatePrecision.UNKNOWN
    schedule_kind: Literal["deadline", "event", "expected_release"] = "deadline"
    status: EventStatus = EventStatus.ACTIVE
    is_optional: bool = Field(default=False, strict=True)
    needs_clarification: bool = Field(default=False, strict=True)
    submission_conditions: str = Field(default="", max_length=3000)
    penalties: str = Field(default="", max_length=2000)
    resubmission_start: datetime | None = None
    resubmission_end: datetime | None = None
    resubmission_precision: Literal["day", "exact", "approximate"] = "day"
    location: str | None = Field(default=None, max_length=500)
    confidence: float = Field(default=0, ge=0, le=1, allow_inf_nan=False, strict=True)
    _timezone = field_validator("timezone")(validate_timezone)

    @field_validator("due_at", "end_at", "resubmission_start", "resubmission_end")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        if value is not None:
            if value.tzinfo is None or value.utcoffset() is None:
                raise ValueError("Дата должна содержать часовой пояс")
            return value.astimezone(UTC)
        return value

    @model_validator(mode="after")
    def precision_matches_date(self) -> "EventData":
        if (self.due_at is None) != (self.date_precision == DatePrecision.UNKNOWN):
            raise ValueError("Неизвестная дата требует unknown, известная — exact/day/approximate")
        if self.end_at and (self.due_at is None or self.end_at < self.due_at):
            raise ValueError("Конец события не может предшествовать началу")
        if self.resubmission_start and self.resubmission_end:
            if self.resubmission_end < self.resubmission_start:
                raise ValueError("Неверный период досдачи")
        if self.schedule_kind == "expected_release" and not self.needs_clarification:
            raise ValueError("Ожидание выдачи требует уточнения")
        return self


class MessageData(StrictModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=False)
    chat_id: int
    chat_title: str = Field(default="", max_length=300)
    message_id: int = Field(gt=0)
    text: str = Field(max_length=16000)
    timestamp: datetime
    edit_timestamp: datetime | None = None
    author_id: int | None = None
    author_name: str | None = Field(default=None, max_length=300)
    author_role: Literal["unknown", "teacher", "assistant", "student"] = "unknown"
    reply_to_message_id: int | None = Field(default=None, gt=0)
    source_url: str | None = None

    @field_validator("timestamp", "edit_timestamp")
    @classmethod
    def aware_utc(cls, value: datetime | None) -> datetime | None:
        return EventData.aware_utc(value)


class MessageRef(StrictModel):
    chat_id: int = Field(strict=True)
    message_id: int = Field(gt=0, strict=True)


class Evidence(StrictModel):
    messages: list[MessageRef] = Field(default_factory=list, max_length=100)
    source_ids: list[UUID] = Field(default_factory=list, max_length=100)
    correction_ids: list[UUID] = Field(default_factory=list, max_length=30)

    @model_validator(mode="after")
    def nonempty(self) -> "Evidence":
        if not (self.messages or self.source_ids or self.correction_ids):
            raise ValueError("Операция требует подтверждающий источник или исправление")
        return self


class CreateEvent(StrictModel):
    op: Literal["create_event"] = "create_event"
    event: EventData
    evidence: Evidence


class TargetOperation(StrictModel):
    event_id: UUID
    expected_version: int = Field(ge=1, strict=True)
    evidence: Evidence
    reason: str = Field(min_length=1, max_length=2000)


class UpdateEvent(TargetOperation):
    op: Literal["update_event"] = "update_event"
    event: EventData


class CancelEvent(TargetOperation):
    op: Literal["cancel_event"] = "cancel_event"


class CompleteEvent(TargetOperation):
    op: Literal["complete_event"] = "complete_event"


class AddSource(TargetOperation):
    op: Literal["add_source"] = "add_source"


class ConflictVariant(StrictModel):
    event: EventData
    evidence: Evidence
    label: str = Field(default="", max_length=300)


class CreateConflict(TargetOperation):
    op: Literal["create_conflict"] = "create_conflict"
    variants: list[ConflictVariant] = Field(min_length=2, max_length=10)

    @model_validator(mode="after")
    def different_states(self) -> "CreateConflict":
        states = [v.event.model_dump(exclude={"confidence"}) for v in self.variants]
        if all(state == states[0] for state in states[1:]):
            raise ValueError("Конфликт требует разные состояния события")
        return self


class NoAction(StrictModel):
    op: Literal["no_action"] = "no_action"
    reason: str = Field(min_length=1, max_length=2000)


Operation = Annotated[
    CreateEvent | UpdateEvent | CancelEvent | CompleteEvent | AddSource | CreateConflict | NoAction,
    Field(discriminator="op"),
]


class AnalysisResult(StrictModel):
    operations: list[Operation] = Field(default_factory=list, max_length=100)


class EventMemory(StrictModel):
    id: UUID
    version: int
    data: EventData
    source_ids: list[UUID] = Field(default_factory=list)


class SourceMemory(StrictModel):
    id: UUID
    message: MessageData
    available: bool = True


class CorrectionMemory(StrictModel):
    id: UUID
    event_id: UUID
    text: str
    created_at: datetime
    status: str


class AnalysisContext(StrictModel):
    timezone: str = "Europe/Moscow"
    study_group: str | None = None
    _timezone = field_validator("timezone")(validate_timezone)
    current_time: datetime = Field(default_factory=utcnow)
    events: list[EventMemory] = Field(default_factory=list)
    sources: list[SourceMemory] = Field(default_factory=list)
    corrections: list[CorrectionMemory] = Field(default_factory=list)
    focus_correction_id: UUID | None = None
    recheck_conflict_id: UUID | None = None
    recheck_event_id: UUID | None = None


class AnalysisOutput(BaseModel):
    result: AnalysisResult
    token_usage: dict[str, Any] = Field(default_factory=dict)


class EntityNotFound(Exception):
    """Сущность отсутствует или принадлежит другому пользователю."""


class StaleState(ValueError):
    """После подготовки результата событие изменилось; требуется новый анализ."""


class InputTooLarge(ValueError):
    """Контекст не помещается в настроенный лимит; данные нельзя молча обрезать."""


class OutputTooLarge(InputTooLarge):
    """The model exhausted its output budget; split the batch without applying partial JSON."""


class AnalysisIdleTimeout(TimeoutError):
    """No sync unit completed within the configured inactivity deadline."""


class AnalysisSliceComplete(Exception):
    """A soft run budget was reached at a safe boundary; the user may continue later."""


class AnalysisValidationError(ValueError):
    """Safe application-authored feedback; never interpolate source or model text."""


class AuthFlowError(ValueError):
    """Безопасное сообщение об ошибке входа, пригодное для показа пользователю."""


class IntegrationUnavailable(Exception):
    """Интеграция не настроена или недоступна."""


class LLMProviderUnavailable(IntegrationUnavailable):
    """A gateway error returned as completion text; never retry as a format repair."""


class TransientIntegrationError(IntegrationUnavailable):
    """A temporary Telegram network/RPC failure suitable for a bounded retry."""


class DeliveryBlocked(IntegrationUnavailable):
    """The user blocked the bot; stop background delivery until they contact it again."""


class SessionExpired(IntegrationUnavailable):
    """Telegram подтвердил потерю авторизации, а не временный сетевой сбой."""


class RateLimited(IntegrationUnavailable):
    def __init__(self, retry_after: int) -> None:
        self.retry_after = max(1, retry_after)
        super().__init__(f"Слишком много попыток. Повторите через {self.retry_after} сек.")
