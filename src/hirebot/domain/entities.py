"""Доменные сущности.

Чистые dataclass-ы без Pydantic и SQLAlchemy: домен не должен зависеть от
способа хранения и от транспорта. Персистентность и валидация внешних данных
живут в адаптерах и в инфраструктурном слое.

Все моменты времени — ``datetime`` с ``tzinfo`` (UTC). Наивные даты запрещены:
именно они дают самые дорогие баги при работе с несколькими часовыми поясами.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from hirebot.domain.enums import (
    BookingStatus,
    CandidateIntent,
    Channel,
    ConversationState,
    DraftStatus,
    MessageDirection,
    MessageKind,
    OutboxStatus,
    ScreeningVerdict,
)
from hirebot.domain.errors import PolicyViolation, SlotConflict


def _utcnow() -> datetime:
    return datetime.now(tz=UTC)


def _ensure_aware(value: datetime, *, name: str) -> datetime:
    if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
        msg = f"{name} должен быть timezone-aware (получено наивное значение)"
        raise ValueError(msg)
    return value


@dataclass(slots=True)
class Vacancy:
    """Вакансия на конкретной площадке."""

    id: UUID
    channel: Channel
    external_id: str
    title: str
    company_id: UUID
    description: str = ""
    city: str | None = None
    is_active: bool = True
    # Критерии скрининга: список словарей {key, title, weight, must_have, expectation}.
    # Хранится как есть, потому что состав критериев у каждой вакансии свой.
    criteria: list[dict[str, Any]] = field(default_factory=list)
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def criteria_weight_sum(self) -> float:
        return sum(float(c.get("weight", 1.0)) for c in self.criteria) or 1.0


@dataclass(slots=True)
class Candidate:
    """Кандидат. Дедупликация — по ``(channel, external_id)``.

    ``phone``/``email`` хранятся в адаптере в зашифрованном виде; в домене
    они уже расшифрованы и используются только для связи.
    """

    id: UUID
    channel: Channel
    external_id: str
    full_name: str
    company_id: UUID
    phone: str | None = None
    email: str | None = None
    city: str | None = None
    created_at: datetime = field(default_factory=_utcnow)

    @property
    def display_name(self) -> str:
        """Имя для обращения в чате.

        Никогда не отдаём пустую строку: «Здравствуйте, !» выглядит как баг
        и мгновенно убивает доверие кандидата.
        """
        name = (self.full_name or "").strip()
        return name.split()[0] if name else "кандидат"


@dataclass(slots=True)
class Resume:
    """Резюме кандидата.

    ``raw_text`` — нормализованный текст; ``parsed`` — структурный разбор.
    ``content_hash`` нужен для кэша LLM: одно и то же резюме не должно
    оплачиваться дважды.
    """

    id: UUID
    candidate_id: UUID
    channel: Channel
    external_id: str | None = None
    raw_text: str = ""
    parsed: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    fetched_at: datetime = field(default_factory=_utcnow)

    @staticmethod
    def content_hash(raw_text: str, *, vacancy_id: UUID | None = None) -> str:
        normalized = " ".join((raw_text or "").lower().split())
        payload = f"{vacancy_id}:{normalized}" if vacancy_id else normalized
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()

    @property
    def total_experience_months(self) -> int:
        value = self.parsed.get("total_experience_months")
        return int(value) if isinstance(value, int | float) else 0


@dataclass(slots=True)
class Message:
    id: UUID
    conversation_id: UUID
    direction: MessageDirection
    body: str
    kind: MessageKind = MessageKind.MANUAL
    external_id: str | None = None
    # True, если текст сгенерирован моделью: нужно для аудита и для разбора инцидентов.
    generated_by_llm: bool = False
    model: str | None = None
    prompt_version: str | None = None
    created_at: datetime = field(default_factory=_utcnow)
    sent_at: datetime | None = None


@dataclass(slots=True)
class Conversation:
    """Диалог «кандидат × вакансия» внутри одной площадки.

    Конечный автомат: ``state`` меняется только через ``hirebot.domain.fsm``.
    """

    id: UUID
    company_id: UUID
    candidate_id: UUID
    vacancy_id: UUID
    channel: Channel
    external_id: str
    state: ConversationState = ConversationState.NEW
    # Сколько сообщений бот отправил без участия человека — основа для лимита.
    auto_messages_sent: int = 0
    last_inbound_at: datetime | None = None
    last_outbound_at: datetime | None = None
    next_action_at: datetime | None = None
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    @property
    def is_terminal(self) -> bool:
        return self.state in {ConversationState.CLOSED, ConversationState.REJECTED}


@dataclass(slots=True, frozen=True)
class ScreeningCriterion:
    """Оценка по одному критерию вакансии."""

    key: str
    title: str
    weight: float
    score: float
    must_have: bool
    evidence: str = ""
    is_gap: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 100.0:
            msg = f"score критерия {self.key!r} вне диапазона 0..100: {self.score}"
            raise ValueError(msg)


@dataclass(slots=True)
class ScreeningResult:
    """Результат скоринга резюме под вакансию."""

    id: UUID
    resume_id: UUID
    vacancy_id: UUID
    candidate_id: UUID
    total_score: float
    verdict: ScreeningVerdict
    criteria: list[ScreeningCriterion] = field(default_factory=list)
    summary: str = ""
    gaps: list[str] = field(default_factory=list)
    strengths: list[str] = field(default_factory=list)
    questions: list[str] = field(default_factory=list)
    model: str = "n/a"
    prompt_version: str = "v1"
    cache_hit: bool = False
    created_at: datetime = field(default_factory=_utcnow)

    @property
    def must_have_failures(self) -> list[ScreeningCriterion]:
        return [c for c in self.criteria if c.must_have and c.is_gap]

    @property
    def needs_human(self) -> bool:
        """Пограничный случай: машина не должна решать такие судьбы сама."""
        return self.verdict in {ScreeningVerdict.WEAK, ScreeningVerdict.NEEDS_REVIEW}


@dataclass(slots=True, frozen=True)
class InterviewSlot:
    """Свободный слот для собеседования (в UTC)."""

    start: datetime
    end: datetime
    manager_id: str
    timezone: str

    def __post_init__(self) -> None:
        _ensure_aware(self.start, name="slot.start")
        _ensure_aware(self.end, name="slot.end")
        if self.end <= self.start:
            msg = "slot.end должен быть позже slot.start"
            raise ValueError(msg)

    @property
    def duration(self) -> timedelta:
        return self.end - self.start

    def overlaps(self, other_start: datetime, other_end: datetime) -> bool:
        return self.start < other_end and other_start < self.end


@dataclass(slots=True, frozen=True)
class BusyInterval:
    """Занятость из календаря. ``manager_id`` может быть не известен при чтении из внешнего календаря."""

    start: datetime
    end: datetime
    manager_id: str | None = None
    title: str | None = None

    def __post_init__(self) -> None:
        _ensure_aware(self.start, name="busy.start")
        _ensure_aware(self.end, name="busy.end")


@dataclass(slots=True)
class Booking:
    """Бронь собеседования.

    Идемпотентность держится на паре ``idempotency_key`` (уникальный индекс в БД)
    и на ``external_meeting_id``, который возвращает календарь.
    """

    id: UUID
    conversation_id: UUID
    candidate_id: UUID
    vacancy_id: UUID
    manager_id: str
    start: datetime
    end: datetime
    timezone: str
    status: BookingStatus = BookingStatus.PROPOSED
    external_meeting_id: str | None = None
    external_event_id: str | None = None
    idempotency_key: str = ""
    created_at: datetime = field(default_factory=_utcnow)
    updated_at: datetime = field(default_factory=_utcnow)

    def __post_init__(self) -> None:
        _ensure_aware(self.start, name="booking.start")
        _ensure_aware(self.end, name="booking.end")


@dataclass(slots=True)
class CandidateReply:
    """Разобранный ответ кандидата."""

    intent: CandidateIntent
    summary: str = ""
    extracted: dict[str, Any] = field(default_factory=dict)
    chosen_slot_index: int | None = None
    confidence: float = 0.0
    raw_text: str = ""


@dataclass(slots=True)
class OutboxEntry:
    """Запись транзакционного outbox.

    Позволяет атомарно «изменить состояние + запланировать отправку»:
    сначала коммит в БД, потом доставка воркером. Без этого либо теряются
    сообщения, либо бот пишет дважды при ретрае.
    """

    id: UUID
    topic: str
    payload: dict[str, Any]
    dedupe_key: str
    status: OutboxStatus = OutboxStatus.PENDING
    attempts: int = 0
    available_at: datetime = field(default_factory=_utcnow)
    last_error: str | None = None
    created_at: datetime = field(default_factory=_utcnow)

    def can_retry(self, max_attempts: int) -> bool:
        return self.attempts < max_attempts


@dataclass(slots=True)
class Company:
    """Компания-арендатор. Основа мультитенантности: все запросы фильтруются по ней."""

    id: UUID
    name: str
    slug: str
    created_at: datetime = field(default_factory=_utcnow)


@dataclass(slots=True)
class DraftMessage:
    """Черновик сообщения кандидату для подтверждения рекрутером.

    Зачем это существует: бот не должен писать реальным кандидатам без человека.
    Но и «бот молчит» — плохо: рекрутер теряет главное преимущество автоматизации.
    Компромисс: бот делает работу (решает, что ответить, и пишет текст), а
    отправка происходит после одного нажатия.

    ``target_state`` — состояние, в которое перейдёт диалог после одобрения.
    Без него нельзя корректно завершить шаг воронки: пока черновик ждал решения,
    диалог находится в ``HUMAN_REVIEW``.
    """

    id: UUID
    conversation_id: UUID
    body: str
    kind: MessageKind
    target_state: ConversationState
    status: DraftStatus = DraftStatus.PENDING
    #: Почему бот предлагает именно это — показывается рекрутеру.
    reason: str = ""
    #: Провенанс текста: чем сгенерирован и по какой версии промпта.
    model: str | None = None
    prompt_version: str | None = None
    generated_by_llm: bool = False
    created_at: datetime = field(default_factory=_utcnow)
    reviewed_at: datetime | None = None
    reviewer: str | None = None
    review_comment: str | None = None

    def __post_init__(self) -> None:
        if not self.body.strip():
            msg = "Черновик не может быть пустым"
            raise ValueError(msg)
        self.body = self.body.strip()

    @property
    def is_pending(self) -> bool:
        return self.status is DraftStatus.PENDING

    def approve(self, reviewer: str, *, text: str | None = None, now: datetime | None = None) -> None:
        """Одобрить черновик. Рекрутер может отредактировать текст перед отправкой."""
        if not self.is_pending:
            msg = f"Черновик уже обработан (статус {self.status.value})"
            raise PolicyViolation(msg)
        if text is not None:
            cleaned = text.strip()
            if not cleaned:
                msg = "Одобренный текст не может быть пустым"
                raise PolicyViolation(msg)
            self.body = cleaned
        self.status = DraftStatus.APPROVED
        self.reviewer = reviewer
        self.reviewed_at = now or _utcnow()

    def reject(self, reviewer: str, *, reason: str | None = None, now: datetime | None = None) -> None:
        """Отклонить черновик: сообщение не отправляется."""
        if not self.is_pending:
            msg = f"Черновик уже обработан (статус {self.status.value})"
            raise PolicyViolation(msg)
        self.status = DraftStatus.REJECTED
        self.reviewer = reviewer
        self.review_comment = reason
        self.reviewed_at = now or _utcnow()

    def supersede(self) -> None:
        """Пометить устаревшим: появилось новое решение бота."""
        if self.is_pending:
            self.status = DraftStatus.SUPERSEDED


def new_id() -> UUID:
    return uuid4()


def make_slot(start: datetime, duration_minutes: int, *, manager_id: str, timezone: str) -> InterviewSlot:
    return InterviewSlot(
        start=start,
        end=start + timedelta(minutes=duration_minutes),
        manager_id=manager_id,
        timezone=timezone,
    )


def assert_slot_is_free(slot: InterviewSlot, busy: list[BusyInterval]) -> None:
    """Гарантия «не бронируем поверх занятого».

    Проверка обязательна на стороне сервиса: календари не всегда валидируют
    пересечения при создании встречи. Гонку между чтением и записью закрывают
    две вещи: ``BookingRepository.find_active_overlap`` (повторная проверка
    занятости в той же транзакции) и частичный уникальный индекс на
    ``bookings (manager_id, starts_at)`` для активных броней.
    """
    for interval in busy:
        if slot.overlaps(interval.start, interval.end):
            msg = (
                f"Слот {slot.start.isoformat()}..{slot.end.isoformat()} "
                f"пересекается с занятостью {interval.start.isoformat()}..{interval.end.isoformat()}"
            )
            raise SlotConflict(msg)


__all__ = [
    "Booking",
    "BookingStatus",
    "BusyInterval",
    "Candidate",
    "CandidateReply",
    "Company",
    "Conversation",
    "DraftMessage",
    "InterviewSlot",
    "Message",
    "OutboxEntry",
    "Resume",
    "ScreeningCriterion",
    "ScreeningResult",
    "Vacancy",
    "assert_slot_is_free",
    "make_slot",
    "new_id",
]
