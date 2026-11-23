"""Оркестратор шага воронки.

Один вызов ``process`` — один шаг: проверить политику, при необходимости
обсчитать резюме, разобрать ответ кандидата и сделать ровно одно осмысленное
действие (пригласить, задать вопрос, предложить слоты, забронировать,
закрыть, увести человеку).

Почему так, а не «всё в одном большом методе»:

* **шаг идемпотентен.** Повторный запуск после падения видит уже изменённое
  состояние и ничего не дублирует;
* **транзакции короткие.** Сетевые вызовы (LLM, календарь, чат площадки)
  выполняются между транзакциями, а не внутри: блокировка строки диалога не
  должна висеть секунды;
* **все переходы состояния — через FSM** (``fsm.assert_transition``), включая
  цепочки вроде ``NEW -> SCREENED -> INVITED``;
* **все исходящие сообщения идемпотентны.** Запись в ``messages`` и смена
  состояния происходят в одной транзакции, а ключ вида
  ``{conversation_id}:{state}:{kind}`` защищает от повторной отправки при
  ретрае.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from hirebot.container import Container
from hirebot.domain.entities import (
    Candidate,
    CandidateReply,
    DraftMessage,
    InterviewSlot,
    Message,
    Resume,
    ScreeningResult,
    Vacancy,
    new_id,
)
from hirebot.domain.enums import (
    CandidateIntent,
    Channel,
    ConversationState,
    MessageDirection,
    MessageKind,
    ProfileStatus,
    ScreeningVerdict,
)
from hirebot.domain.errors import (
    ChannelNotConfigured,
    GuardrailViolation,
    LlmError,
    NotFound,
    PolicyViolation,
)
from hirebot.domain.scoring import ScoringProfile
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_message_sent
from hirebot.ports.channels import ChannelPort
from hirebot.ports.dto import (
    InboundMessage,
    SentMessage,
)
from hirebot.ports.llm import MessageDraft, MessageRequest, ReplyAnalysisRequest
from hirebot.ports.repositories import UnitOfWork
from hirebot.services.base import DEFAULT_TIMEZONE, ServiceBase
from hirebot.services.messaging import (
    build_idempotency_key,
    count_outbound_kind,
    format_slot_moment,
    format_slots,
)
from hirebot.services.policy import PolicyGuard
from hirebot.services.scheduling import DEFAULT_SLOT_LIMIT, SchedulingService
from hirebot.services.scoring_profile import ScoringProfileService
from hirebot.services.screening import NO_RESUME_ID, ScreeningService
from hirebot.services.transitions import apply_transition

logger = get_logger(__name__)

#: Состояния, в которых диалог уже идёт и ответы кандидата имеют смысл.
DIALOGUE_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.QUESTIONS_SENT,
        ConversationState.AWAITING_ANSWER,
        ConversationState.INVITED,
        ConversationState.AWAITING_SLOT_CHOICE,
        ConversationState.BOOKED,
    }
)

#: Состояния «первого контакта»: решение по кандидату ещё не принято.
FIRST_CONTACT_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.NEW,
        ConversationState.RESUME_FETCHED,
        ConversationState.SCREENED,
    }
)

#: Состояния, в которых осмыслен выбор слота из предложенных.
SLOT_CHOICE_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.INVITED,
        ConversationState.AWAITING_SLOT_CHOICE,
        ConversationState.BOOKED,
    }
)

#: Сколько сообщений истории отдаём модели для контекста.
HISTORY_FOR_LLM = 10

#: Сколько сообщений читаем из БД, чтобы понять, что уже было в диалоге.
#: Жёсткий предел вопросов кандидату: больше пяти не задаём никогда.
MAX_PROFILE_QUESTIONS = 5

MESSAGE_HISTORY_LIMIT = 100

#: Максимальная длина исходящего сообщения (совпадает с дефолтом ``MessageRequest``).
MAX_MESSAGE_CHARS = 900

#: Сколько уточняющих вопросов задаём до человека. Один: если после
#: уточняющего вопроса ответ снова неясен, разбираться должен рекрутер,
#: а не бот, который переспрашивает по кругу.
MAX_CLARIFYING_ROUNDS = 1

_ORDINALS: dict[str, int] = {
    "первый": 1,
    "второй": 2,
    "третий": 3,
    "четвертый": 4,
    "пятый": 5,
    "first": 1,
    "second": 2,
    "third": 3,
    "fourth": 4,
    "fifth": 5,
}

_EXPLICIT_NUMBER_RE = re.compile(r"(?:вариант|слот|номер|№)\s*(\d{1,2})")
_LEADING_NUMBER_RE = re.compile(r"^\s*(\d{1,2})\s*[).]?\s")


def slot_index_from_text(text: str, count: int) -> int | None:
    """Найти номер выбранного слота в свободном тексте (нумерация с единицы).

    Это страховка на случай, когда модель не заполнила ``chosen_slot_index``:
    кандидат пишет «беру второй» или «вариант 3», и мы обязаны понять выбор.
    """
    if count <= 0 or not text:
        return None
    normalized = text.lower().replace("ё", "е")

    explicit = _EXPLICIT_NUMBER_RE.search(normalized)
    if explicit is not None:
        number = int(explicit.group(1))
        return number - 1 if 1 <= number <= count else None

    leading = _LEADING_NUMBER_RE.match(normalized)
    if leading is not None:
        number = int(leading.group(1))
        if 1 <= number <= count:
            return number - 1

    for word, number in _ORDINALS.items():
        if word in normalized and 1 <= number <= count:
            return number - 1
    return None


def resolve_slot_index(reply: CandidateReply, count: int) -> int | None:
    """Определить индекс выбранного слота.

    Приоритет — значение от модели (``chosen_slot_index``, нумерация с нуля).
    Если модель вернула 1-based значение вне диапазона, пробуем прочитать его
    как номер варианта. Выбор слота рассматривается только при интенте
    ``INTERESTED``: иначе «слот 2 не подходит» превратился бы в бронь.
    """
    if count <= 0 or reply.intent is not CandidateIntent.INTERESTED:
        return None
    if reply.chosen_slot_index is not None:
        index = reply.chosen_slot_index
        if 0 <= index < count:
            return index
        if 1 <= index <= count:
            return index - 1
    return slot_index_from_text(reply.raw_text, count)


@dataclass(slots=True)
class ConversationOutcome:
    """Итог шага воронки."""

    conversation_id: UUID
    state: ConversationState
    action_taken: str
    detail: str = ""
    used_llm: bool = False


@dataclass(slots=True)
class _Outbound:
    """Сообщение, которое нужно отправить в рамках шага."""

    kind: MessageKind
    text: str
    generated_by_llm: bool = False
    model: str | None = None
    prompt_version: str | None = None


@dataclass(slots=True)
class _Step:
    """Запланированный шаг: куда перейти и что отправить."""

    expected_state: ConversationState
    target_state: ConversationState
    action_taken: str
    outbound: list[_Outbound] = field(default_factory=list)
    inbound: list[InboundMessage] = field(default_factory=list)
    detail: str = ""


@dataclass(slots=True)
class _Context:
    """Всё, что шаг воронки читает перед принятием решения."""

    conversation_id: UUID
    channel: Channel
    external_id: str
    state: ConversationState
    auto_messages_sent: int
    last_inbound_at: datetime | None
    candidate: Candidate | None
    vacancy: Vacancy
    resume: Resume | None
    screening: ScreeningResult | None
    messages: list[Message]
    timezone: str
    profile: ScoringProfile | None = None

    @property
    def display_name(self) -> str:
        return self.candidate.display_name if self.candidate is not None else "кандидат"


class ConversationService(ServiceBase):
    """Шаг воронки: скрининг резюме, разбор ответа, приглашение, бронь."""

    def __init__(self, container: Container) -> None:
        super().__init__(container)
        self._policy = PolicyGuard(container.settings.policy)
        self._screening = ScreeningService(container)
        self._scheduling = SchedulingService(container)

    async def process(self, conversation_id: UUID) -> ConversationOutcome:
        """Выполнить один шаг воронки для диалога.

        Нарушение политики не выбрасывается наружу: диалог уходит в
        ``HUMAN_REVIEW``, потому что кандидата нельзя ни бросить, ни завалить
        письмами. Наружу уходят только инфраструктурные ошибки (канал,
        календарь) — их ретраит воркер.
        """
        context = await self._load(conversation_id)
        if context is None:
            return ConversationOutcome(
                conversation_id=conversation_id,
                state=ConversationState.NEW,
                action_taken="not_found",
                detail="диалог не найден",
            )
        if context.state in {ConversationState.CLOSED, ConversationState.REJECTED}:
            return ConversationOutcome(
                conversation_id=context.conversation_id,
                state=context.state,
                action_taken="terminal",
                detail=f"диалог в терминальном состоянии {context.state.value}",
            )

        try:
            self._policy.assert_can_send(
                state=context.state,
                auto_messages_sent=context.auto_messages_sent,
                now=self.now(),
                candidate_timezone=context.timezone,
            )
        except PolicyViolation as exc:
            return await self._force_human_review(
                conversation_id, reason=str(exc), action="human_review_policy"
            )

        used_llm = False
        screening = context.screening
        current_state = context.state
        if screening is None:
            try:
                screening = await self._screening.screen(conversation_id=conversation_id)
            except PolicyViolation as exc:
                return await self._force_human_review(
                    conversation_id,
                    reason=f"критерии вакансии нарушают политику: {exc}",
                    action="human_review_criteria",
                )
            except LlmError as exc:
                return await self._force_human_review(
                    conversation_id,
                    reason=f"LLM недоступна: {exc}",
                    action="human_review_llm",
                )
            used_llm = True
            if screening.resume_id != NO_RESUME_ID and current_state in FIRST_CONTACT_STATES:
                await self._mark_screened(conversation_id)
                current_state = ConversationState.SCREENED

        # Shadow-режим профиля: система считает и показывает рекомендацию, но
        # ничего не отправляет. Это режим проверки на реальных кандидатах —
        # без него первый запуск рисковал бы репутацией.
        live_profile = await ScoringProfileService(self._container).get_live(context.vacancy.id)
        if live_profile is not None and live_profile.status is ProfileStatus.SHADOW:
            decision = self._decide_shadow(live_profile, screening)
            logger.info(
                "shadow_decision",
                conversation_id=str(conversation_id),
                percent=decision.percent,
                recommendation=decision.decision.value,
            )
            return await self._force_human_review(
                conversation_id,
                reason=(
                    f"режим проверки профиля: рекомендация «{decision.decision.value}», "
                    f"соответствие {decision.percent:.0f}%, "
                    f"уверенность {decision.coverage:.0%}. Отправка выключена."
                ),
                action="shadow_mode",
            )

        new_inbound: list[InboundMessage] = []
        if current_state in DIALOGUE_STATES:
            new_inbound = await self._fetch_new_inbound(context)
        if new_inbound:
            proposals: list[InterviewSlot] = []
            if current_state in SLOT_CHOICE_STATES:
                proposals = await self._scheduling.propose_slots(
                    conversation_id=conversation_id, limit=DEFAULT_SLOT_LIMIT
                )
            reply = await self._analyze_reply(context, new_inbound, proposals)
            used_llm = True
            step = await self._plan_reply(
                context=context,
                reply=reply,
                proposals=proposals,
                new_inbound=new_inbound,
                current_state=current_state,
            )
            return await self._finalize(conversation_id, step, used_llm=used_llm)

        if current_state in FIRST_CONTACT_STATES:
            step = await self._plan_first_contact(context, screening, current_state)
            return await self._finalize(conversation_id, step, used_llm=used_llm)

        return ConversationOutcome(
            conversation_id=conversation_id,
            state=current_state,
            action_taken="noop",
            detail="нет новых событий",
            used_llm=used_llm,
        )

    # ------------------------------------------------------------------ #
    # Планирование шага
    # ------------------------------------------------------------------ #

    async def _plan_first_contact(
        self,
        context: _Context,
        screening: ScreeningResult | None,
        current_state: ConversationState,
    ) -> _Step:
        if screening is None:
            return _Step(current_state, ConversationState.HUMAN_REVIEW, "human_review_no_screening")
        # При включённом подтверждении решение всё равно рассчитывается: черновик
        # должен содержать осмысленный текст и целевое состояние. Отправку
        # заменит ``_commit_step``, а не ранний выход в HUMAN_REVIEW.
        return await self._plan_after_screening(
            context=context,
            screening=screening,
            current_state=current_state,
            first_contact=True,
            inbound=[],
        )

    async def _plan_after_screening(
        self,
        *,
        context: _Context,
        screening: ScreeningResult,
        current_state: ConversationState,
        first_contact: bool,
        inbound: list[InboundMessage],
    ) -> _Step:
        """Решение по результату скрининга.

        На первом контакте положительный вердикт ведёт в ``INVITED``, а в уже
        идущем диалоге — сразу к выбору слота: приглашать второй раз нельзя.
        """
        if screening.needs_human:
            return _Step(
                current_state,
                ConversationState.HUMAN_REVIEW,
                "human_review_weak",
                inbound=inbound,
                detail=(
                    f"вердикт {screening.verdict.value}, балл {screening.total_score}: "
                    "пограничный случай, решает человек"
                ),
            )

        if screening.verdict is ScreeningVerdict.REJECT:
            if not self.settings.policy.auto_reject_enabled:
                return _Step(
                    current_state,
                    ConversationState.HUMAN_REVIEW,
                    "human_review_reject",
                    inbound=inbound,
                    detail=(f"балл {screening.total_score} ниже порога, но автоотказ выключен политикой"),
                )
            text, draft = await self._compose(
                context,
                "rejection",
                {"score": screening.total_score, "gaps": list(screening.gaps)},
            )
            return _Step(
                current_state,
                ConversationState.REJECTED,
                "rejected",
                outbound=[_outbound_from_draft(MessageKind.REJECTION, text, draft)],
                inbound=inbound,
                detail=f"автоотказ по баллу {screening.total_score}",
            )

        slots = await self._scheduling.propose_slots(
            conversation_id=context.conversation_id, limit=DEFAULT_SLOT_LIMIT
        )
        if not slots:
            return _Step(
                current_state,
                ConversationState.HUMAN_REVIEW,
                "human_review_no_slots",
                inbound=inbound,
                detail="календарь не сконфигурирован или свободных слотов нет",
            )

        questions = self._questions_for(context, screening)
        if first_contact and screening.verdict is ScreeningVerdict.MATCH and questions:
            text, draft = await self._compose(context, "clarifying_question", {"questions": list(questions)})
            return _Step(
                current_state,
                ConversationState.QUESTIONS_SENT,
                "questions_sent",
                outbound=[_outbound_from_draft(MessageKind.CLARIFYING_QUESTION, text, draft)],
                inbound=inbound,
                detail=f"вопросов: {len(screening.questions)}",
            )

        slot_lines = format_slots(slots, timezone=context.timezone)
        if first_contact:
            text, draft = await self._compose(
                context, "invitation", {"slots": slot_lines, "score": screening.total_score}
            )
            return _Step(
                current_state,
                ConversationState.INVITED,
                "invited",
                outbound=[_outbound_from_draft(MessageKind.INVITATION, text, draft)],
                inbound=inbound,
                detail=f"балл {screening.total_score}, слотов предложено {len(slots)}",
            )
        text, draft = await self._compose(
            context, "slot_proposal", {"slots": slot_lines, "score": screening.total_score}
        )
        return _Step(
            current_state,
            ConversationState.AWAITING_SLOT_CHOICE,
            "slots_proposed",
            outbound=[_outbound_from_draft(MessageKind.SLOT_PROPOSAL, text, draft)],
            inbound=inbound,
            detail=f"балл {screening.total_score}, слотов предложено {len(slots)}",
        )

    async def _plan_reply(
        self,
        *,
        context: _Context,
        reply: CandidateReply,
        proposals: list[InterviewSlot],
        new_inbound: list[InboundMessage],
        current_state: ConversationState,
    ) -> _Step:
        conversation_id = context.conversation_id

        index = resolve_slot_index(reply, len(proposals))
        if index is not None and current_state in SLOT_CHOICE_STATES:
            booking = await self._scheduling.book(
                conversation_id=conversation_id, slot_index=index, requested_by="bot"
            )
            slot = InterviewSlot(
                start=booking.start,
                end=booking.end,
                manager_id=booking.manager_id,
                timezone=booking.timezone,
            )
            text, draft = await self._compose(
                context,
                "confirmation",
                {"slot": format_slot_moment(slot, timezone=context.timezone)},
            )
            return _Step(
                ConversationState.BOOKED,
                ConversationState.BOOKED,
                "booked",
                outbound=[_outbound_from_draft(MessageKind.CONFIRMATION, text, draft)],
                inbound=new_inbound,
                detail=f"бронь {booking.id} на {booking.start.isoformat()}",
            )

        intent = reply.intent
        if intent is CandidateIntent.NOT_INTERESTED:
            return _Step(
                current_state,
                ConversationState.CLOSED,
                "closed_by_candidate",
                inbound=new_inbound,
                detail="кандидат сообщил, что не заинтересован",
            )

        if intent is CandidateIntent.RESCHEDULE_REQUEST:
            slots = await self._propose_alternatives(conversation_id)
            if not slots:
                return _Step(
                    current_state,
                    ConversationState.HUMAN_REVIEW,
                    "human_review_no_slots",
                    inbound=new_inbound,
                    detail="не удалось подобрать другие слоты",
                )
            text, draft = await self._compose(
                context, "slot_proposal", {"slots": format_slots(slots, timezone=context.timezone)}
            )
            return _Step(
                current_state,
                ConversationState.AWAITING_SLOT_CHOICE,
                "reschedule_proposed",
                outbound=[_outbound_from_draft(MessageKind.SLOT_PROPOSAL, text, draft)],
                inbound=new_inbound,
                detail=f"альтернативных слотов: {len(slots)}",
            )

        if intent is CandidateIntent.INTERESTED:
            slots = proposals
            if not slots:
                slots = await self._scheduling.propose_slots(
                    conversation_id=conversation_id, limit=DEFAULT_SLOT_LIMIT
                )
            if not slots:
                return _Step(
                    current_state,
                    ConversationState.HUMAN_REVIEW,
                    "human_review_no_slots",
                    inbound=new_inbound,
                    detail="календарь не сконфигурирован или свободных слотов нет",
                )
            text, draft = await self._compose(
                context, "slot_proposal", {"slots": format_slots(slots, timezone=context.timezone)}
            )
            return _Step(
                current_state,
                ConversationState.AWAITING_SLOT_CHOICE,
                "slots_proposed",
                outbound=[_outbound_from_draft(MessageKind.SLOT_PROPOSAL, text, draft)],
                inbound=new_inbound,
                detail=f"слотов предложено {len(slots)}",
            )

        if intent is CandidateIntent.QUESTION:
            text, draft = await self._compose(
                context,
                "question_answer",
                {
                    "question": reply.raw_text,
                    "vacancy_description": context.vacancy.description,
                },
            )
            return _Step(
                current_state,
                current_state,
                "answered_question",
                outbound=[_outbound_from_draft(MessageKind.MANUAL, text, draft)],
                inbound=new_inbound,
                detail="ответ на вопрос кандидата",
            )

        if intent is CandidateIntent.UNCLEAR:
            asked = count_outbound_kind(context.messages, MessageKind.CLARIFYING_QUESTION)
            if asked >= MAX_CLARIFYING_ROUNDS:
                return _Step(
                    current_state,
                    ConversationState.HUMAN_REVIEW,
                    "human_review_unclear",
                    inbound=new_inbound,
                    detail=f"после уточняющего вопроса ответ снова неясен ({asked}): нужен человек",
                )
            text, draft = await self._compose(
                context,
                "clarifying_question",
                {"questions": ["Подскажите, пожалуйста, какое время вам удобно?"]},
            )
            return _Step(
                current_state,
                ConversationState.AWAITING_ANSWER,
                "clarification_asked",
                outbound=[_outbound_from_draft(MessageKind.CLARIFYING_QUESTION, text, draft)],
                inbound=new_inbound,
                detail="уточняющий вопрос",
            )

        # PROVIDED_INFO: кандидат прислал новую информацию — пересчитываем
        # скоринг и продолжаем воронку.
        return await self._plan_rescreened(context, current_state, new_inbound)

    async def _plan_rescreened(
        self,
        context: _Context,
        current_state: ConversationState,
        new_inbound: list[InboundMessage],
    ) -> _Step:
        try:
            screening = await self._screening.screen(conversation_id=context.conversation_id)
        except PolicyViolation as exc:
            return _Step(
                current_state,
                ConversationState.HUMAN_REVIEW,
                "human_review_criteria",
                inbound=new_inbound,
                detail=f"критерии вакансии нарушают политику: {exc}",
            )
        except LlmError as exc:
            return _Step(
                current_state,
                ConversationState.HUMAN_REVIEW,
                "human_review_llm",
                inbound=new_inbound,
                detail=f"LLM недоступна: {exc}",
            )
        return await self._plan_after_screening(
            context=context,
            screening=screening,
            current_state=current_state,
            first_contact=False,
            inbound=new_inbound,
        )

    async def _propose_alternatives(self, conversation_id: UUID) -> list[InterviewSlot]:
        """Слоты «на замену»: смещаемся по той же детерминированной сетке.

        Порт не даёт места, где хранить ранее предложенные слоты, поэтому
        альтернативы — это хвост той же сетки. Список стабилен: сетка строится
        от одного и того же окна, пока диалог не «переехал» в следующий час.
        """
        slots = await self._scheduling.propose_slots(
            conversation_id=conversation_id, limit=DEFAULT_SLOT_LIMIT * 2
        )
        alternatives = slots[DEFAULT_SLOT_LIMIT:]
        return alternatives or slots

    # ------------------------------------------------------------------ #
    # Исполнение шага
    # ------------------------------------------------------------------ #

    async def _finalize(
        self,
        conversation_id: UUID,
        step: _Step,
        *,
        used_llm: bool,
    ) -> ConversationOutcome:
        """Проверить guardrail и выполнить шаг."""
        problem = self._guardrail_problem(step.outbound)
        if problem is not None:
            logger.warning(
                "guardrail_blocked",
                conversation_id=str(conversation_id),
                problem=problem,
            )
            return await self._force_human_review(conversation_id, reason=problem, action="guardrail_blocked")
        return await self._commit_step(conversation_id, step, used_llm=used_llm)

    def _guardrail_problem(self, outbound: list[_Outbound]) -> str | None:
        for item in outbound:
            try:
                self._policy.assert_text_allowed(item.text)
            except GuardrailViolation as exc:
                return str(exc)
        return None

    async def _commit_step(
        self,
        conversation_id: UUID,
        step: _Step,
        *,
        used_llm: bool,
    ) -> ConversationOutcome:
        """Отправить сообщения, записать их и сменить состояние — в одной транзакции.

        Состояние перечитывается заново: если между планированием и записью
        диалог ушёл дальше (например, параллельный воркер успел пригласить
        кандидата), шаг отменяется без побочных эффектов.
        """
        async with self.uow() as uow:
            conversation = await uow.conversations.get_with_lock(conversation_id)
            if conversation is None:
                return ConversationOutcome(
                    conversation_id=conversation_id,
                    state=step.expected_state,
                    action_taken="not_found",
                    detail="диалог не найден",
                    used_llm=used_llm,
                )
            if conversation.state is not step.expected_state:
                return ConversationOutcome(
                    conversation_id=conversation.id,
                    state=conversation.state,
                    action_taken="state_changed",
                    detail=(f"ожидалось {step.expected_state.value}, получено {conversation.state.value}"),
                    used_llm=used_llm,
                )

            stored = list(
                await uow.messages.list_for_conversation(conversation_id, limit=MESSAGE_HISTORY_LIMIT)
            )
            now = self.now()

            for inbound in step.inbound:
                message = await self._store_inbound(uow, conversation.id, inbound, stored)
                if message is not None:
                    stored.append(message)
            if step.inbound:
                latest = max(item.created_at for item in step.inbound)
                previous = conversation.last_inbound_at
                conversation.last_inbound_at = latest if previous is None else max(previous, latest)

            # Режим обязательного подтверждения: бот уже решил, что ответить,
            # и написал текст, но кандидату ничего не уходит. Вместо отправки
            # готовим черновик, а диалог остаётся человеку.
            #
            # Канал здесь намеренно не нужен: ничего не отправляется. Это
            # позволяет готовить черновики даже при недоступной площадке, а
            # значит и без действующих токенов — например, в демо и на ревью.
            if self.settings.policy.require_human_approval and step.outbound:
                for item in step.outbound:
                    await uow.drafts.add(
                        DraftMessage(
                            id=new_id(),
                            conversation_id=conversation.id,
                            body=item.text,
                            kind=item.kind,
                            target_state=step.target_state,
                            reason=step.detail or step.action_taken,
                            model=item.model,
                            prompt_version=item.prompt_version,
                            generated_by_llm=item.generated_by_llm,
                            created_at=now,
                        )
                    )
                conversation.state = apply_transition(conversation.state, ConversationState.HUMAN_REVIEW)
                conversation.updated_at = now
                await uow.conversations.upsert(conversation)
                await uow.commit()

                logger.info(
                    "conversation_step_drafted",
                    conversation_id=str(conversation_id),
                    drafts=len(step.outbound),
                    target_state=step.target_state.value,
                    action=step.action_taken,
                )
                return ConversationOutcome(
                    conversation_id=conversation_id,
                    state=conversation.state,
                    action_taken=f"{step.action_taken}_draft" if step.action_taken else "draft",
                    detail=(f"черновик готов к подтверждению: {step.detail or step.action_taken}"),
                    used_llm=used_llm,
                )

            # Канал нужен только для отправки. При пустом плане (например,
            # календарь недоступен и время предложить нечем) обращаться к нему
            # бессмысленно — и вредно: выключенная площадка роняла бы шаг.
            channel = self._channel(conversation.channel) if step.outbound else None
            for item in step.outbound:
                if channel is None:  # pragma: no cover - защита от регресса
                    break
                ordinal = 1 + count_outbound_kind(stored, item.kind)
                idempotency_key = build_idempotency_key(conversation.id, item.kind, ordinal=ordinal)
                sent = await channel.send_message(
                    conversation.external_id,
                    item.text,
                    idempotency_key=idempotency_key,
                    kind_hint=item.kind.value,
                )
                message = await self._store_outbound(uow, conversation.id, item, sent, now)
                record_message_sent(conversation.channel.value, item.kind.value)
                if message is not None:
                    stored.append(message)
                    conversation.auto_messages_sent += 1
                    conversation.last_outbound_at = sent.sent_at or now

            conversation.state = apply_transition(conversation.state, step.target_state)
            conversation.updated_at = now
            await uow.conversations.upsert(conversation)
            await uow.commit()

        logger.info(
            "conversation_step_done",
            conversation_id=str(conversation_id),
            state=conversation.state.value,
            action=step.action_taken,
            auto_messages_sent=conversation.auto_messages_sent,
        )
        return ConversationOutcome(
            conversation_id=conversation_id,
            state=conversation.state,
            action_taken=step.action_taken,
            detail=step.detail,
            used_llm=used_llm,
        )

    async def _store_inbound(
        self,
        uow: UnitOfWork,
        conversation_id: UUID,
        inbound: InboundMessage,
        stored: list[Message],
    ) -> Message | None:
        if inbound.external_id and any(item.external_id == inbound.external_id for item in stored):
            return None
        message = Message(
            id=new_id(),
            conversation_id=conversation_id,
            direction=MessageDirection.INBOUND,
            body=inbound.text,
            kind=MessageKind.MANUAL,
            external_id=inbound.external_id or None,
            created_at=inbound.created_at,
            sent_at=inbound.created_at,
        )
        return await uow.messages.add(message)

    async def _store_outbound(
        self,
        uow: UnitOfWork,
        conversation_id: UUID,
        item: _Outbound,
        sent: SentMessage,
        now: datetime,
    ) -> Message | None:
        if sent.external_id and await uow.messages.exists_external_id(conversation_id, sent.external_id):
            # Сообщение уже записано прошлым заходом: не пишем второй раз и не
            # увеличиваем счётчик автосообщений.
            logger.info(
                "outbound_already_stored",
                conversation_id=str(conversation_id),
                external_id=sent.external_id,
            )
            return None
        message = Message(
            id=new_id(),
            conversation_id=conversation_id,
            direction=MessageDirection.OUTBOUND,
            body=item.text,
            kind=item.kind,
            external_id=sent.external_id,
            generated_by_llm=item.generated_by_llm,
            model=item.model,
            prompt_version=item.prompt_version,
            created_at=now,
            sent_at=sent.sent_at or now,
        )
        return await uow.messages.add(message)

    async def _mark_screened(self, conversation_id: UUID) -> None:
        """Зафиксировать веху ``SCREENED`` отдельной транзакцией.

        Веха нужна как точка возобновления: если следующий шаг упадёт (например,
        календарь недоступен), повторный запуск не будет заново платить за
        скрининг и не потеряет факт «резюме разобрано».
        """
        async with self.uow() as uow:
            conversation = await uow.conversations.get_with_lock(conversation_id)
            if conversation is None or conversation.state is ConversationState.SCREENED:
                return
            if conversation.state not in FIRST_CONTACT_STATES:
                logger.debug(
                    "screened_mark_skipped",
                    conversation_id=str(conversation_id),
                    state=conversation.state.value,
                )
                return
            conversation.state = apply_transition(conversation.state, ConversationState.SCREENED)
            conversation.updated_at = self.now()
            await uow.conversations.upsert(conversation)
            await uow.commit()

    async def _force_human_review(
        self,
        conversation_id: UUID,
        *,
        reason: str,
        action: str,
    ) -> ConversationOutcome:
        """Увести диалог к человеку. Наружу ничего не бросаем — это штатная ветка."""
        async with self.uow() as uow:
            conversation = await uow.conversations.get_with_lock(conversation_id)
            if conversation is None:
                return ConversationOutcome(
                    conversation_id=conversation_id,
                    state=ConversationState.NEW,
                    action_taken="not_found",
                    detail="диалог не найден",
                )
            if conversation.state is ConversationState.HUMAN_REVIEW:
                return ConversationOutcome(
                    conversation_id=conversation.id,
                    state=conversation.state,
                    action_taken="already_human_review",
                    detail=reason,
                )
            if conversation.is_terminal:
                return ConversationOutcome(
                    conversation_id=conversation.id,
                    state=conversation.state,
                    action_taken="terminal",
                    detail=reason,
                )
            conversation.state = apply_transition(conversation.state, ConversationState.HUMAN_REVIEW)
            conversation.updated_at = self.now()
            await uow.conversations.upsert(conversation)
            await uow.commit()

        logger.warning(
            "human_review_required",
            conversation_id=str(conversation_id),
            action=action,
            reason=reason,
        )
        return ConversationOutcome(
            conversation_id=conversation_id,
            state=ConversationState.HUMAN_REVIEW,
            action_taken=action,
            detail=reason,
        )

    # ------------------------------------------------------------------ #
    # Чтение и вызовы внешнего мира
    # ------------------------------------------------------------------ #

    def _questions_for(self, context: _Context, screening: ScreeningResult | None) -> list[str]:
        """Вопросы кандидату: из профиля скоринга, иначе из скрининга.

        Профиль — настройка рекрутера: он задаёт и текст вопроса, и критерий,
        который тот уточняет. Скрининг остаётся запасным источником для вакансий
        без профиля.

        Лимит берётся из настроек профиля: «не больше пяти» — это правило
        вакансии, а не константа в коде.
        """
        if context.profile is not None and context.profile.questions:
            limit = context.profile.thresholds.max_questions
            texts = [item.text for item in context.profile.questions][:limit]
            if texts:
                return texts
        if screening is None:
            return []
        limit = self.settings.policy.max_auto_messages_per_candidate
        return list(screening.questions)[: min(limit, MAX_PROFILE_QUESTIONS)]

    def _decide_shadow(self, profile: Any, screening: ScreeningResult | None) -> Any:
        """Рекомендация системы в shadow-режиме (без отправки кандидату).

        Ошибки метрики не должны ломать шаг: если посчитать не удалось, шаг всё
        равно уходит человеку — это безопасное поведение.
        """
        from hirebot.domain.enums import DecisionKind
        from hirebot.domain.scoring import DecisionOutcome

        if screening is None:
            return DecisionOutcome(
                decision=DecisionKind.HUMAN,
                percent=0.0,
                coverage=0.0,
                blockers=(),
                stop_reason="shadow",
                explanation="скрининг недоступен",
            )
        percent = screening.match_percent
        coverage = screening.confidence
        blockers = tuple(screening.blockers)
        decision = profile.thresholds.decide(percent=percent, coverage=coverage, has_blocker=bool(blockers))
        return DecisionOutcome(
            decision=decision,
            percent=percent,
            coverage=coverage,
            blockers=blockers,
            stop_reason="shadow",
        )

    async def _load(self, conversation_id: UUID) -> _Context | None:
        async with self.uow() as uow:
            conversation = await uow.conversations.get_with_lock(conversation_id)
            if conversation is None:
                return None
            vacancy = await uow.vacancies.get(conversation.vacancy_id)
            if vacancy is None:
                msg = f"Вакансия {conversation.vacancy_id} не найдена"
                raise NotFound(msg)
            candidate = await uow.candidates.get(conversation.candidate_id)
            resume = await uow.resumes.get_latest_for_candidate(conversation.candidate_id)
            screening = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
            messages = list(
                await uow.messages.list_for_conversation(conversation_id, limit=MESSAGE_HISTORY_LIMIT)
            )

        # Профиль нужен и для инвалидации скрининга, и для вопросов кандидату,
        # поэтому читаем его один раз независимо от наличия скрининга.
        live_profile = await ScoringProfileService(self._container).get_live(conversation.vacancy_id)
        # Если профиль скоринга изменился, прежний результат больше не отражает
        # настройку: считаем, что скрининга нет, и оценим заново. Иначе кандидат
        # получил бы решение по старым критериям.
        if screening is not None:
            current_version = live_profile.version if live_profile is not None else None
            if screening.profile_version != current_version:
                logger.info(
                    "screening_invalidated_by_profile_change",
                    conversation_id=str(conversation_id),
                    old_version=screening.profile_version,
                    new_version=current_version,
                )
                screening = None
        return _Context(
            conversation_id=conversation.id,
            channel=conversation.channel,
            external_id=conversation.external_id,
            state=conversation.state,
            auto_messages_sent=conversation.auto_messages_sent,
            last_inbound_at=conversation.last_inbound_at,
            candidate=candidate,
            vacancy=vacancy,
            resume=resume,
            screening=screening,
            messages=messages,
            profile=live_profile,
            timezone=candidate_timezone(resume, self.settings.talantix.default_timezone),
        )

    async def _fetch_new_inbound(self, context: _Context) -> list[InboundMessage]:
        """Новые входящие сообщения площадки, которых ещё нет в БД.

        Ошибку канала не глотаем: если чат недоступен, мы не знаем, ответил
        кандидат или нет, и «продолжить как обычно» означало бы потерять ответ
        или отправить дубль. Пусть воркер повторит шаг.

        Исключение — **канал не подключён вовсе** (``ChannelNotConfigured``):
        это не сбой, а состояние конфигурации. Тогда работаем по истории из БД:
        для подготовки черновика на подтверждение канал не нужен, а падать из-за
        отсутствия токенов в демо и на ревью бессмысленно.
        """
        try:
            channel = self._channel(context.channel)
        except ChannelNotConfigured as exc:
            logger.warning(
                "inbound_read_skipped",
                channel=context.channel.value,
                reason=str(exc),
            )
            return []
        messages = await channel.list_messages(context.external_id, since=context.last_inbound_at)
        known = {message.external_id for message in context.messages if message.external_id}
        fresh: list[InboundMessage] = []
        for message in messages:
            if message.direction is not MessageDirection.INBOUND:
                continue
            if message.external_id and message.external_id in known:
                continue
            fresh.append(message)
        return fresh

    async def _analyze_reply(
        self,
        context: _Context,
        new_inbound: list[InboundMessage],
        proposals: list[InterviewSlot],
    ) -> CandidateReply:
        last = new_inbound[-1]
        request = ReplyAnalysisRequest(
            candidate_name=context.display_name,
            reply_text=last.text,
            question_asked=last_clarifying_question(context.messages),
            proposed_slots=tuple(format_slots(proposals, timezone=context.timezone)),
        )
        try:
            reply = await self._container.llm.analyze_reply(request)
        except LlmError as exc:
            logger.warning(
                "reply_analysis_failed",
                conversation_id=str(context.conversation_id),
                error=str(exc),
            )
            return CandidateReply(
                intent=CandidateIntent.UNCLEAR,
                summary=f"LLM недоступна: {exc}",
                raw_text=last.text,
            )
        if not reply.raw_text:
            reply.raw_text = last.text
        return reply

    async def _compose(
        self,
        context: _Context,
        kind: str,
        extra: dict[str, Any],
    ) -> tuple[str, MessageDraft | None]:
        """Составить текст сообщения; при сбое модели — детерминированный шаблон.

        Факты о вакансии модель получает только из ``vacancy.description``:
        выдумывать условия работы нельзя, это дороже, чем показаться неумным.
        """
        request = MessageRequest(
            kind=kind,
            candidate_name=context.display_name,
            vacancy_title=context.vacancy.title,
            company_name="",
            context=dict(extra),
            history=self._history(context.messages),
            max_chars=MAX_MESSAGE_CHARS,
        )
        try:
            draft = await self._container.llm.compose_message(request)
        except LlmError as exc:
            logger.warning(
                "compose_message_failed",
                conversation_id=str(context.conversation_id),
                kind=kind,
                error=str(exc),
            )
            return fallback_text(kind, extra), None
        text = draft.text.strip()
        if not text:
            return fallback_text(kind, extra), None
        return _ensure_slots_in_text(text, extra), draft

    def _history(self, messages: list[Message]) -> list[dict[str, str]]:
        history: list[dict[str, str]] = []
        for message in messages[-HISTORY_FOR_LLM:]:
            role = "candidate" if message.direction is MessageDirection.INBOUND else "recruiter"
            history.append({"role": role, "text": message.body})
        return history

    def _channel(self, name: Channel) -> ChannelPort:
        try:
            return self._container.channel(name)
        except (ChannelNotConfigured, KeyError) as exc:
            raise ChannelNotConfigured(str(exc)) from exc


def _outbound_from_draft(kind: MessageKind, text: str, draft: MessageDraft | None) -> _Outbound:
    return _Outbound(
        kind=kind,
        text=text,
        generated_by_llm=draft is not None,
        model=draft.model if draft is not None else None,
        prompt_version=draft.prompt_version if draft is not None else None,
    )


def _ensure_slots_in_text(text: str, extra: dict[str, Any]) -> str:
    """Гарантировать, что список слотов попал в текст целиком.

    Нумерация — единственный способ для кандидата выбрать время, поэтому
    «модель забыла строку» здесь недопустимо: дописываем список сами.
    """
    slots = extra.get("slots")
    if not isinstance(slots, list) or not slots:
        return text
    lines = [str(line) for line in slots]
    if all(line in text for line in lines):
        return text
    return f"{text}\n\n" + "\n".join(lines)


def candidate_timezone(resume: Resume | None, default: str) -> str:
    """Часовой пояс кандидата: из разобранного резюме, иначе — из настроек."""
    if resume is not None:
        value = resume.parsed.get("timezone")
        if isinstance(value, str) and value.strip():
            return value.strip()
    return default or DEFAULT_TIMEZONE


def last_clarifying_question(messages: list[Message]) -> str:
    """Последний заданный уточняющий вопрос — контекст для разбора ответа."""
    for message in reversed(messages):
        if message.direction is MessageDirection.OUTBOUND and message.kind is MessageKind.CLARIFYING_QUESTION:
            return message.body
    return ""


def fallback_text(kind: str, extra: dict[str, Any]) -> str:
    """Детерминированный шаблон на случай недоступности модели.

    Это не «второй LLM», а страховка: шаблон честно описывает следующий шаг и
    не выдумывает ни одного факта о вакансии.
    """
    slots = extra.get("slots")
    slot_lines = "\n".join(str(line) for line in slots) if isinstance(slots, list) else ""
    if kind == "invitation":
        return (
            "Здравствуйте! Спасибо за отклик. Предлагаю обсудить детали на созвоне. "
            f"Доступное время (конфликта в календаре нет):\n{slot_lines}\n"
            "Напишите, пожалуйста, номер удобного варианта."
        )
    if kind == "slot_proposal":
        return f"Здравствуйте! Вот варианты времени:\n{slot_lines}\nНапишите номер удобного."
    questions = extra.get("questions")
    questions_text = (
        "\n".join(f"— {question}" for question in questions) if isinstance(questions, list) else ""
    )
    if kind == "clarifying_question":
        return f"Здравствуйте! Чтобы двигаться дальше, уточните, пожалуйста:\n{questions_text}"
    if kind == "question_answer":
        return (
            "Спасибо за вопрос! Этой информации нет в описании вакансии, "
            "поэтому уточню у рекрутера и вернусь с ответом."
        )
    if kind == "confirmation":
        moment = extra.get("slot", "согласованное время")
        return f"Готово! Записал вас на {moment}. Если планы изменятся — напишите, перенесём."
    if kind == "rejection":
        return "Спасибо за отклик и потраченное время. К сожалению, по этой вакансии мы не можем продолжить."
    return "Спасибо за ответ! Вернусь с деталями в ближайшее время."
