"""Pydantic-схемы HTTP API: запросы, ответы и ошибки.

Правила слоя доставки, которые обеспечиваются этим модулем:

* наружу уходят только описанные здесь модели — доменные dataclass-ы и
  результаты сервисов не сериализуются «как есть»;
* контакты кандидата **маскируются**: даже административный API не отдаёт
  телефон и e-mail целиком (152-ФЗ, принцип минимизации данных);
* в теле ошибки нет текста исключения — только код, ``error_id`` и общее
  сообщение (детали остаются в логе).

Схемы намеренно «плоские»: они описывают контракт HTTP, а не домен, поэтому
меняются вместе с API, не задевая сервисы.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from hirebot.domain.entities import (
    Booking,
    Candidate,
    Conversation,
    InterviewSlot,
    Message,
    ScreeningResult,
)
from hirebot.domain.enums import (
    BookingStatus,
    Channel,
    ConversationState,
    MessageDirection,
    MessageKind,
    ScreeningVerdict,
)

# ---------------------------------------------------------------------------
# Вспомогательные функции маскирования контактов
# ---------------------------------------------------------------------------


def mask_email(value: str | None) -> str | None:
    """Оставить от адреса только домен и первую букву локальной части."""
    if not value:
        return None
    local, _, domain = value.partition("@")
    if not domain:
        return "***"
    return f"{local[:1] or '*'}***@{domain}"


def mask_phone(value: str | None) -> str | None:
    """Оставить от номера только две последние цифры."""
    if not value:
        return None
    digits = "".join(char for char in value if char.isdigit())
    return f"***{digits[-2:]}" if digits else "***"


# ---------------------------------------------------------------------------
# Служебные ответы
# ---------------------------------------------------------------------------


class ErrorResponse(BaseModel):
    """Единый формат любой ошибки API.

    ``error_id`` — единственный ключ к подробностям: по нему в логе находится
    полная информация, включая текст исключения и трассировку.
    """

    error_id: str = Field(description="Идентификатор инцидента для поиска в логах")
    code: str = Field(description="Машиночитаемый код ошибки")
    message: str = Field(description="Общее описание без деталей и персональных данных")
    retry_after_s: float | None = Field(
        default=None, description="Через сколько секунд имеет смысл повторить запрос"
    )


class HealthResponse(BaseModel):
    """Ответ liveness-проверки: процесс жив и отвечает."""

    status: Literal["ok"] = "ok"
    service: str = "hirebot"
    version: str


class ComponentHealth(BaseModel):
    """Состояние одной зависимости (БД, канал, Redis)."""

    name: str
    ok: bool
    detail: str | None = None


class ReadyResponse(BaseModel):
    """Ответ readiness-проверки: можно ли принимать трафик."""

    status: Literal["ready", "degraded"]
    database: ComponentHealth
    channels: list[ComponentHealth] = Field(default_factory=list)
    queue_backend: str
    llm_provider: str
    calendar_configured: bool


class VersionResponse(BaseModel):
    """Версия сборки и окружение — нужна для разбора инцидентов."""

    name: str = "hirebot"
    version: str
    env: str
    python: str


# ---------------------------------------------------------------------------
# Обобщённые результаты шагов воронки
# ---------------------------------------------------------------------------


class ActionResult(BaseModel):
    """Результат шага воронки в обобщённом виде.

    Сервисы возвращают собственные типы (``PollResult``, ``ConversationOutcome``),
    которые слою доставки знать не обязательно. Наружу уходят только числовые
    счётчики и, если получится, состояние диалога: так в ответ не попадут
    персональные данные кандидата, случайно оказавшиеся в результате.
    """

    status: str = "ok"
    counters: dict[str, int] = Field(default_factory=dict)
    state: ConversationState | None = None
    correlation_id: str | None = None


class PollResponse(ActionResult):
    """Результат ручного поллинга площадки."""

    channel: Channel


class WebhookAccepted(BaseModel):
    """Ответ на уведомление площадки: принято и поставлено в очередь."""

    status: Literal["accepted"] = "accepted"
    channel: Channel
    job: str
    event_id: str
    duplicate: bool = False


# ---------------------------------------------------------------------------
# Доменные представления
# ---------------------------------------------------------------------------


class CandidateSchema(BaseModel):
    """Кандидат в административном API.

    Контакты отдаются только в маскированном виде: рекрутеру достаточно понять,
    что канал связи есть, а полный номер он получит в UI площадки.
    """

    id: UUID
    channel: Channel
    full_name: str
    city: str | None = None
    phone_masked: str | None = None
    email_masked: str | None = None
    has_phone: bool = False
    has_email: bool = False
    created_at: datetime

    @classmethod
    def from_entity(cls, candidate: Candidate) -> CandidateSchema:
        """Собрать схему из доменной сущности, замаскировав контакты."""
        return cls(
            id=candidate.id,
            channel=candidate.channel,
            full_name=candidate.full_name,
            city=candidate.city,
            phone_masked=mask_phone(candidate.phone),
            email_masked=mask_email(candidate.email),
            has_phone=bool(candidate.phone),
            has_email=bool(candidate.email),
            created_at=candidate.created_at,
        )


class VacancySchema(BaseModel):
    """Вакансия: карточка для административного API."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel: Channel
    external_id: str
    title: str
    city: str | None = None
    is_active: bool = True
    criteria: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime


class ConversationSummary(BaseModel):
    """Краткая карточка диалога для списков."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    channel: Channel
    state: ConversationState
    external_id: str
    candidate_id: UUID
    vacancy_id: UUID
    auto_messages_sent: int
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    next_action_at: datetime | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, conversation: Conversation) -> ConversationSummary:
        return cls.model_validate(conversation)


class MessageSchema(BaseModel):
    """Сообщение в переписке."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    direction: MessageDirection
    kind: MessageKind
    body: str
    external_id: str | None = None
    generated_by_llm: bool = False
    model: str | None = None
    prompt_version: str | None = None
    created_at: datetime
    sent_at: datetime | None = None

    @classmethod
    def from_entity(cls, message: Message) -> MessageSchema:
        return cls.model_validate(message)


class ScreeningCriterionSchema(BaseModel):
    """Оценка по одному критерию вакансии."""

    model_config = ConfigDict(from_attributes=True)

    key: str
    title: str
    weight: float
    score: float
    must_have: bool
    evidence: str = ""
    is_gap: bool = False


class ScreeningSchema(BaseModel):
    """Результат скрининга резюме."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    resume_id: UUID
    vacancy_id: UUID
    candidate_id: UUID
    total_score: float
    verdict: ScreeningVerdict
    criteria: list[ScreeningCriterionSchema] = Field(default_factory=list)
    summary: str = ""
    gaps: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)
    model: str = "n/a"
    prompt_version: str = "v1"
    cache_hit: bool = False
    created_at: datetime

    @classmethod
    def from_entity(cls, result: ScreeningResult) -> ScreeningSchema:
        return cls.model_validate(result)


class SlotSchema(BaseModel):
    """Свободный слот собеседования (UTC)."""

    start: datetime
    end: datetime
    duration_minutes: int
    manager_id: str
    timezone: str

    @classmethod
    def from_entity(cls, slot: InterviewSlot) -> SlotSchema:
        return cls(
            start=slot.start,
            end=slot.end,
            duration_minutes=int(slot.duration.total_seconds() // 60),
            manager_id=slot.manager_id,
            timezone=slot.timezone,
        )


class BookingSchema(BaseModel):
    """Бронь собеседования."""

    model_config = ConfigDict(from_attributes=True)

    id: UUID
    conversation_id: UUID
    candidate_id: UUID
    vacancy_id: UUID
    manager_id: str
    start: datetime
    end: datetime
    timezone: str
    status: BookingStatus
    external_meeting_id: str | None = None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_entity(cls, booking: Booking) -> BookingSchema:
        return cls.model_validate(booking)


class ConversationCard(BaseModel):
    """Полная карточка диалога: кандидат, вакансия, скрининг, переписка, бронь."""

    conversation: ConversationSummary
    candidate: CandidateSchema | None = None
    vacancy: VacancySchema | None = None
    screening: ScreeningSchema | None = None
    messages: list[MessageSchema] = Field(default_factory=list)
    booking: BookingSchema | None = None


class ConversationListResponse(BaseModel):
    """Постраничный список диалогов."""

    items: list[ConversationSummary] = Field(default_factory=list)
    limit: int
    offset: int
    count: int


class ManualMessageAccepted(BaseModel):
    """Ручное сообщение рекрутера принято и положено в outbox."""

    status: Literal["queued"] = "queued"
    message_id: UUID
    outbox_entry_id: UUID
    conversation_id: UUID
    requested_by: str


# ---------------------------------------------------------------------------
# Запросы
# ---------------------------------------------------------------------------


class ManualMessageRequest(BaseModel):
    """Ручное сообщение рекрутера кандидату."""

    text: str = Field(min_length=1, max_length=4000, description="Текст сообщения")
    requested_by: str = Field(
        default="recruiter", max_length=120, description="Кто инициировал отправку (аудит)"
    )


class SlotProposalRequest(BaseModel):
    """Параметры подбора слотов собеседования."""

    days_ahead: int = Field(default=5, ge=1, le=90)
    limit: int = Field(default=3, ge=1, le=10)


class BookRequest(BaseModel):
    """Бронирование одного из предложенных слотов.

    Время можно указать явно (``slot_start``) или индексом в текущей сетке
    (``slot_index``). Явное время надёжнее: индекс указывает на позицию в
    **пересчитанной** сетке, поэтому между предложением слотов и подтверждением
    он может указать на другое время — например, если календарь уже отдал это
    время занятым. Если переданы оба поля, приоритет у ``slot_start``.
    """

    slot_start: datetime | None = Field(
        default=None,
        description="Начало выбранного слота (ISO 8601, с часовым поясом)",
    )
    slot_index: int = Field(default=0, ge=0, le=10, description="Индекс слота из ответа /slots")
    requested_by: str = Field(default="bot", max_length=120)


class WebhookPayload(BaseModel):
    """Уведомление площадки.

    Схема намеренно терпимая (``extra="allow"``): форматы вебхуков у площадок
    меняются, а неизвестные поля нужно сохранить для разбора. Полезные поля
    извлекаются по нескольким возможным именам — см. ``routes_webhooks``.
    """

    model_config = ConfigDict(extra="allow")

    event_id: str | None = None
    id: str | None = None
    event: str | None = None
    event_type: str | None = None
    conversation_id: UUID | None = None
    vacancy_id: str | None = None
    response_id: str | None = None


class DraftSchema(BaseModel):
    """Черновик сообщения в очереди подтверждения."""

    draft_id: UUID
    conversation_id: UUID
    candidate_name: str
    vacancy_title: str
    channel: str
    body: str
    kind: str
    reason: str = ""
    model: str | None = None
    created_at: datetime
    score: float | None = None


class DraftListResponse(BaseModel):
    """Очередь подтверждения."""

    total: int
    items: list[DraftSchema]


class ApproveDraftRequest(BaseModel):
    """Одобрение черновика. ``text`` позволяет отправить отредактированную версию."""

    text: str | None = Field(default=None, max_length=2000)
    reviewer: str = Field(default="recruiter", max_length=120)


class RejectDraftRequest(BaseModel):
    """Отклонение черновика."""

    reason: str | None = Field(default=None, max_length=500)
    reviewer: str = Field(default="recruiter", max_length=120)


class ApprovedDraftResponse(BaseModel):
    """Результат одобрения: сообщение отправлено и шаг воронки завершён."""

    draft_id: UUID
    conversation_id: UUID
    message_id: UUID
    external_id: str | None = None
    state: str


class CriterionConfigSchema(BaseModel):
    """Критерий в профиле скоринга."""

    key: str = Field(min_length=1, max_length=64)
    title: str = Field(min_length=1, max_length=255)
    weight: int = Field(default=3, ge=1, le=5)
    must_have: bool = False
    evidence_hint: str = ""
    expectation: str = ""
    source: str = "catalog"


class QuestionConfigSchema(BaseModel):
    """Вопрос профиля, привязанный к критерию."""

    text: str = Field(min_length=1, max_length=500)
    criterion_key: str = Field(min_length=1, max_length=64)
    kind: str = "fact"
    informativeness: int = Field(default=2, ge=1, le=3)
    is_custom: bool = False


class ThresholdsSchema(BaseModel):
    """Пороги и режим решения."""

    invite_at: float = Field(default=85.0, ge=0, le=100)
    human_at: float = Field(default=55.0, ge=0, le=100)
    reject_at: float = Field(default=40.0, ge=0, le=100)
    mode: str = "absolute"
    top_k: int = Field(default=10, ge=1, le=1000)
    max_questions: int = Field(default=5, ge=1, le=5)
    auto_invite: bool = False
    min_coverage_for_auto: float = Field(default=0.7, ge=0, le=1)


class ScoringProfileSchema(BaseModel):
    """Профиль скоринга вакансии."""

    id: UUID
    vacancy_id: UUID
    version: int
    status: str
    criteria: list[CriterionConfigSchema]
    questions: list[QuestionConfigSchema]
    thresholds: ThresholdsSchema
    created_by: str
    comment: str = ""
    published_at: datetime | None = None


class ScoringProfileDraftRequest(BaseModel):
    """Сохранить черновик профиля."""

    criteria: list[CriterionConfigSchema] = Field(min_length=1)
    questions: list[QuestionConfigSchema] = Field(default_factory=list)
    thresholds: ThresholdsSchema = Field(default_factory=ThresholdsSchema)
    created_by: str = Field(default="recruiter", max_length=120)
    comment: str = Field(default="", max_length=500)


class PublishProfileRequest(BaseModel):
    """Опубликовать профиль. ``as_shadow`` — режим проверки без отправки."""

    as_shadow: bool = True


class VacancyCreateRequest(BaseModel):
    """Создать вакансию в системе."""

    title: str = Field(min_length=1, max_length=255)
    description: str = Field(default="", max_length=8000)
    city: str | None = Field(default=None, max_length=120)
    company_id: UUID | None = None
    channel: str = "hh"


class QueueItemSchema(BaseModel):
    """Кандидат в очереди по приоритету."""

    conversation_id: UUID
    candidate_name: str
    state: str
    percent: float
    coverage: float
    priority: float
    blockers: list[str] = Field(default_factory=list)
    age_hours: float | None = None
    contributions: dict[str, float] = Field(default_factory=dict)
    vacancy_title: str = ""


class QueueResponse(BaseModel):
    total: int
    items: list[QueueItemSchema]


__all__ = [
    "ActionResult",
    "ApproveDraftRequest",
    "ApprovedDraftResponse",
    "BookRequest",
    "BookingSchema",
    "CandidateSchema",
    "ComponentHealth",
    "ConversationCard",
    "ConversationListResponse",
    "ConversationSummary",
    "CriterionConfigSchema",
    "DraftListResponse",
    "DraftSchema",
    "ErrorResponse",
    "HealthResponse",
    "ManualMessageAccepted",
    "ManualMessageRequest",
    "MessageSchema",
    "PollResponse",
    "PublishProfileRequest",
    "QuestionConfigSchema",
    "QueueItemSchema",
    "QueueResponse",
    "ReadyResponse",
    "RejectDraftRequest",
    "ScoringProfileDraftRequest",
    "ScoringProfileSchema",
    "ScreeningCriterionSchema",
    "ScreeningSchema",
    "SlotProposalRequest",
    "SlotSchema",
    "ThresholdsSchema",
    "VacancyCreateRequest",
    "VacancySchema",
    "VersionResponse",
    "WebhookAccepted",
    "WebhookPayload",
    "mask_email",
    "mask_phone",
]
