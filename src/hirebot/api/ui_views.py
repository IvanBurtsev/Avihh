"""Сборка представлений для UI рекрутера.

Зачем отдельный модуль: шаблоны не должны ходить в репозитории, а роутеры —
заниматься форматированием. Здесь ровно одна ответственность: прочитать нужные
сущности в одной транзакции и превратить их в плоские структуры для шаблона.

Все человекочитаемые подписи (состояния, вердикты, формат времени) собраны
здесь же — иначе один и тот же ``human_review`` назывался бы по-разному в разных
местах интерфейса.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hirebot.domain.entities import Booking, Conversation, Message, ScreeningResult
from hirebot.domain.enums import ConversationState, MessageDirection, ScreeningVerdict
from hirebot.domain.errors import NotFound
from hirebot.ports.repositories import UnitOfWork

#: Подписи состояний диалога.
STATE_LABELS: dict[ConversationState, str] = {
    ConversationState.NEW: "Новый отклик",
    ConversationState.RESUME_FETCHED: "Резюме загружено",
    ConversationState.SCREENED: "Скрининг выполнен",
    ConversationState.QUESTIONS_SENT: "Вопросы отправлены",
    ConversationState.AWAITING_ANSWER: "Ждём ответ",
    ConversationState.AWAITING_SLOT_CHOICE: "Выбор времени",
    ConversationState.INVITED: "Приглашён",
    ConversationState.BOOKED: "Собеседование назначено",
    ConversationState.REJECTED: "Отказ",
    ConversationState.HUMAN_REVIEW: "Нужен человек",
    ConversationState.CLOSED: "Закрыт",
}

#: Подписи вердиктов скрининга.
VERDICT_LABELS: dict[str, str] = {
    ScreeningVerdict.STRONG_MATCH.value: "Сильное соответствие",
    ScreeningVerdict.MATCH.value: "Соответствует",
    ScreeningVerdict.WEAK.value: "Слабое соответствие",
    ScreeningVerdict.REJECT.value: "Не подходит",
    ScreeningVerdict.NEEDS_REVIEW.value: "На проверку",
}

#: Подписи типов сообщений.
MESSAGE_KIND_LABELS: dict[str, str] = {
    "greeting": "Приветствие",
    "clarifying_question": "Уточняющий вопрос",
    "invitation": "Приглашение",
    "slot_proposal": "Предложение времени",
    "confirmation": "Подтверждение",
    "rejection": "Отказ",
    "reminder": "Напоминание",
    "manual": "Сообщение рекрутера",
}

#: Состояния, в которых человеку действительно нужен диалог.
HUMAN_STATES: frozenset[ConversationState] = frozenset({ConversationState.HUMAN_REVIEW})

#: Порядок счётчиков в шапке очереди.
COUNTER_ORDER: tuple[tuple[ConversationState, str], ...] = (
    (ConversationState.NEW, "Новые"),
    (ConversationState.AWAITING_ANSWER, "Ждут ответа"),
    (ConversationState.AWAITING_SLOT_CHOICE, "Выбирают время"),
    (ConversationState.HUMAN_REVIEW, "Нужен человек"),
    (ConversationState.BOOKED, "Назначено"),
)

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


def state_label(state: ConversationState) -> str:
    return STATE_LABELS.get(state, state.value)


def verdict_label(verdict: str) -> str:
    return VERDICT_LABELS.get(verdict, verdict)


@dataclass(slots=True)
class CounterView:
    state: str
    label: str
    count: int


@dataclass(slots=True)
class QueueRow:
    conversation_id: UUID
    candidate_name: str
    vacancy_title: str
    channel: str
    state: str
    state_label: str
    score: float | None
    verdict: str
    verdict_label: str
    last_message: str
    updated_at: str
    needs_human: bool


@dataclass(slots=True)
class CriterionView:
    title: str
    weight: float
    score: float
    must_have: bool
    is_gap: bool
    evidence: str


@dataclass(slots=True)
class ScreeningView:
    score: float
    verdict: str
    verdict_label: str
    summary: str
    criteria: list[CriterionView]
    gaps: list[str]
    questions: list[str]
    model: str
    prompt_version: str
    #: Процент соответствия и уверенность: два числа, потому что одно врёт.
    percent: float | None = None
    coverage: float | None = None
    blockers: list[str] = field(default_factory=list)
    profile_version: int | None = None

    @property
    def confidence_label(self) -> str:
        """Уверенность словами: рекрутеру понятнее, чем дробь."""
        if self.coverage is None:
            return "—"
        percent = round(self.coverage * 100)
        if percent >= 80:
            return f"высокая ({percent}%)"
        if percent >= 50:
            return f"средняя ({percent}%)"
        return f"низкая ({percent}%)"

    @property
    def needs_more_data(self) -> bool:
        return self.coverage is not None and self.coverage < 0.7


@dataclass(slots=True)
class MessageView:
    author: str
    body: str
    created_at: str
    is_outbound: bool
    kind: str


@dataclass(slots=True)
class BookingView:
    when: str
    external_meeting_id: str | None


@dataclass(slots=True)
class SlotView:
    when: str
    start_iso: str
    manager_id: str


@dataclass(slots=True)
class DraftView:
    """Черновик в очереди подтверждения."""

    draft_id: UUID
    conversation_id: UUID
    candidate_name: str
    vacancy_title: str
    channel: str
    body: str
    kind: str
    kind_label: str
    reason: str
    model: str | None
    created_at: str
    score: float | None


@dataclass(slots=True)
class ConversationView:
    conversation_id: UUID
    candidate_name: str
    vacancy_title: str
    channel: str
    state: str
    state_label: str
    has_inbound: bool
    screening: ScreeningView | None = None
    messages: list[MessageView] = field(default_factory=list)
    booking: BookingView | None = None
    slots: list[SlotView] = field(default_factory=list)
    resume_text: str | None = None
    #: Ожидающий подтверждения черновик (если включён режим апрува).
    draft: DraftView | None = None
    #: Можно ли отправлять сообщения: канал должен быть подключён. Если нет,
    #: форма скрывается, иначе рекрутер нажимает кнопку и получает ошибку.
    can_send: bool = True
    #: Причина, по которой отправка недоступна (показывается вместо формы).
    send_blocked_reason: str = ""


def _zone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):  # pragma: no cover - защита от плохой настройки
        return ZoneInfo("UTC")


def format_moment(value: datetime | None, *, timezone: str = "Europe/Moscow") -> str:
    """Дата и время в часовом поясе рекрутера (в БД всё в UTC)."""
    if value is None:
        return "—"
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    local = aware.astimezone(_zone(timezone))
    return f"{WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y %H:%M}"


def format_slot(value: datetime, *, timezone: str = "Europe/Moscow") -> str:
    aware = value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    local = aware.astimezone(_zone(timezone))
    return f"{WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y в %H:%M} ({timezone})"


def _short(text: str, *, limit: int = 120) -> str:
    flat = " ".join((text or "").split())
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def _has_pending_inbound(messages: list[Message]) -> bool:
    """Ждёт ли ответ кандидата реакции: последнее сообщение пришло от него."""
    if not messages:
        return False
    last = max(messages, key=lambda item: item.created_at)
    return last.direction is MessageDirection.INBOUND


def screening_view(result: ScreeningResult) -> ScreeningView:
    return ScreeningView(
        score=result.total_score,
        verdict=result.verdict.value,
        verdict_label=verdict_label(result.verdict.value),
        summary=result.summary,
        criteria=[
            CriterionView(
                title=criterion.title,
                weight=criterion.weight,
                score=criterion.score,
                must_have=criterion.must_have,
                is_gap=criterion.is_gap,
                evidence=criterion.evidence,
            )
            for criterion in result.criteria
        ],
        gaps=list(result.gaps),
        questions=list(result.questions),
        model=result.model,
        prompt_version=result.prompt_version,
        percent=result.match_percent,
        coverage=result.coverage,
        blockers=list(result.blockers),
        profile_version=result.profile_version,
    )


def message_view(message: Message, *, timezone: str) -> MessageView:
    return MessageView(
        author="Рекрутер" if message.direction is MessageDirection.OUTBOUND else "Кандидат",
        body=message.body,
        created_at=format_moment(message.created_at, timezone=timezone),
        is_outbound=message.direction is MessageDirection.OUTBOUND,
        kind=message.kind.value if message.kind else "",
    )


def booking_view(booking: Booking, *, timezone: str) -> BookingView:
    return BookingView(
        when=format_slot(booking.start, timezone=booking.timezone or timezone),
        external_meeting_id=booking.external_meeting_id,
    )


async def build_queue(
    uow: UnitOfWork,
    *,
    state: ConversationState | None = None,
    limit: int = 100,
    timezone: str = "Europe/Moscow",
) -> tuple[list[QueueRow], list[CounterView]]:
    """Собрать очередь диалогов и счётчики по состояниям."""
    states = [state] if state is not None else list(ConversationState)
    conversations = list(await uow.conversations.list_by_state(states, limit=limit))

    counters: list[CounterView] = []
    for counter_state, label in COUNTER_ORDER:
        count = len(await uow.conversations.list_by_state([counter_state], limit=1000))
        counters.append(CounterView(state=counter_state.value, label=label, count=count))

    return (
        [await _queue_row(uow, conversation, timezone=timezone) for conversation in conversations],
        counters,
    )


async def _queue_row(uow: UnitOfWork, conversation: Conversation, *, timezone: str) -> QueueRow:
    candidate = await uow.candidates.get(conversation.candidate_id)
    vacancy = await uow.vacancies.get(conversation.vacancy_id)
    screening = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
    messages = list(await uow.messages.list_for_conversation(conversation.id, limit=1))

    last_message = ""
    if messages:
        last = max(messages, key=lambda item: item.created_at)
        prefix = "Вы: " if last.direction is MessageDirection.OUTBOUND else ""
        last_message = prefix + _short(last.body)

    return QueueRow(
        conversation_id=conversation.id,
        candidate_name=candidate.full_name if candidate is not None else "Без имени",
        vacancy_title=vacancy.title if vacancy is not None else "—",
        channel=conversation.channel.value,
        state=conversation.state.value,
        state_label=state_label(conversation.state),
        score=screening.total_score if screening is not None else None,
        verdict=screening.verdict.value if screening is not None else "",
        verdict_label=verdict_label(screening.verdict.value) if screening is not None else "—",
        last_message=last_message or "—",
        updated_at=format_moment(conversation.updated_at, timezone=timezone),
        needs_human=conversation.state in HUMAN_STATES,
    )


async def build_conversation(
    uow: UnitOfWork, conversation_id: UUID, *, timezone: str = "Europe/Moscow"
) -> ConversationView:
    """Собрать карточку диалога.

    Отсутствующий диалог — это ``NotFound``, а не пустая карточка: иначе на
    несуществующий адрес интерфейс отвечал бы «всё хорошо».
    """
    conversation = await uow.conversations.get(conversation_id)
    if conversation is None:
        msg = f"Диалог {conversation_id} не найден"
        raise NotFound(msg)

    candidate = await uow.candidates.get(conversation.candidate_id)
    vacancy = await uow.vacancies.get(conversation.vacancy_id)
    screening = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
    messages = list(await uow.messages.list_for_conversation(conversation.id, limit=200))
    booking = await uow.bookings.get_for_conversation(conversation.id)
    resume = await uow.resumes.get_latest_for_candidate(conversation.candidate_id)
    pending = await uow.drafts.get_pending_for_conversation(conversation.id)

    return ConversationView(
        conversation_id=conversation.id,
        candidate_name=candidate.full_name if candidate is not None else "Без имени",
        vacancy_title=vacancy.title if vacancy is not None else "—",
        channel=conversation.channel.value,
        state=conversation.state.value,
        state_label=state_label(conversation.state),
        has_inbound=_has_pending_inbound(messages),
        screening=screening_view(screening) if screening is not None else None,
        messages=[message_view(message, timezone=timezone) for message in messages],
        booking=booking_view(booking, timezone=timezone) if booking is not None else None,
        slots=[],
        resume_text=(resume.raw_text or None) if resume is not None else None,
        draft=_draft_view_from_entity(pending, timezone=timezone) if pending is not None else None,
    )


def slots_view(slots: Any, *, timezone: str = "Europe/Moscow") -> list[SlotView]:
    """Предложенные слоты для отображения."""
    views: list[SlotView] = []
    for slot in slots:
        views.append(
            SlotView(
                when=format_slot(slot.start, timezone=slot.timezone or timezone),
                start_iso=slot.start.isoformat(),
                manager_id=slot.manager_id,
            )
        )
    return views


def _draft_view_from_entity(draft: Any, *, timezone: str = "Europe/Moscow") -> DraftView:
    """Черновик из доменной сущности (``DraftMessage``) для карточки диалога."""
    return DraftView(
        draft_id=draft.id,
        conversation_id=draft.conversation_id,
        candidate_name="",
        vacancy_title="",
        channel="",
        body=draft.body,
        kind=draft.kind.value,
        kind_label=kind_label(draft.kind.value),
        reason=draft.reason,
        model=draft.model,
        created_at=format_moment(draft.created_at, timezone=timezone),
        score=None,
    )


def kind_label(kind: str) -> str:
    """Человеческое название типа сообщения."""
    return MESSAGE_KIND_LABELS.get(kind, kind)


def draft_view(draft: Any, *, timezone: str = "Europe/Moscow") -> DraftView:
    """Представление черновика для шаблона.

    ``draft`` — ``PendingDraft`` из ``ApprovalService``; принимаем ``Any``,
    чтобы слой представлений не зависел от сервисного пакета.
    """
    return DraftView(
        draft_id=draft.draft_id,
        conversation_id=draft.conversation_id,
        candidate_name=draft.candidate_name,
        vacancy_title=draft.vacancy_title,
        channel=draft.channel,
        body=draft.body,
        kind=draft.kind,
        kind_label=kind_label(draft.kind),
        reason=draft.reason,
        model=draft.model,
        created_at=format_moment(draft.created_at, timezone=timezone),
        score=draft.score,
    )


__all__ = [
    "COUNTER_ORDER",
    "MESSAGE_KIND_LABELS",
    "STATE_LABELS",
    "VERDICT_LABELS",
    "BookingView",
    "ConversationView",
    "CounterView",
    "CriterionView",
    "DraftView",
    "MessageView",
    "QueueRow",
    "ScreeningView",
    "SlotView",
    "booking_view",
    "build_conversation",
    "build_queue",
    "draft_view",
    "format_moment",
    "format_slot",
    "kind_label",
    "message_view",
    "screening_view",
    "slots_view",
    "state_label",
    "verdict_label",
]
