"""Доменные перечисления.

Осознанно на ``str``-основе: значения попадают в БД и в JSON как есть,
а не как магические числа.
"""

from __future__ import annotations

from enum import StrEnum


class Channel(StrEnum):
    """Площадка, с которой пришёл кандидат."""

    HH = "hh"
    AVITO = "avito"


class ConversationState(StrEnum):
    """Состояние диалога с кандидатом (конечный автомат воронки).

    Переходы описаны в ``hirebot.domain.fsm``; произвольная смена состояния
    запрещена — это гарантия, что бот не напишет кандидату дважды один и тот же
    вопрос и не пригласит на собес уже отказника.
    """

    NEW = "new"
    RESUME_FETCHED = "resume_fetched"
    SCREENED = "screened"
    QUESTIONS_SENT = "questions_sent"
    AWAITING_ANSWER = "awaiting_answer"
    AWAITING_SLOT_CHOICE = "awaiting_slot_choice"
    INVITED = "invited"
    BOOKED = "booked"
    REJECTED = "rejected"
    HUMAN_REVIEW = "human_review"
    CLOSED = "closed"


class MessageDirection(StrEnum):
    INBOUND = "inbound"
    OUTBOUND = "outbound"


class MessageKind(StrEnum):
    """Тип сообщения — влияет на шаблоны и на счётчик автосообщений."""

    GREETING = "greeting"
    CLARIFYING_QUESTION = "clarifying_question"
    INVITATION = "invitation"
    SLOT_PROPOSAL = "slot_proposal"
    CONFIRMATION = "confirmation"
    REJECTION = "rejection"
    REMINDER = "reminder"
    MANUAL = "manual"


class ScreeningVerdict(StrEnum):
    STRONG_MATCH = "strong_match"
    MATCH = "match"
    WEAK = "weak"
    REJECT = "reject"
    NEEDS_REVIEW = "needs_review"


class CandidateIntent(StrEnum):
    """Классификация ответа кандидата. Нужна, чтобы отвечать по делу."""

    INTERESTED = "interested"
    QUESTION = "question"
    PROVIDED_INFO = "provided_info"
    RESCHEDULE_REQUEST = "reschedule_request"
    NOT_INTERESTED = "not_interested"
    UNCLEAR = "unclear"


class BookingStatus(StrEnum):
    PROPOSED = "proposed"
    CONFIRMED = "confirmed"
    CANCELLED = "cancelled"
    COMPLETED = "completed"
    NO_SHOW = "no_show"


class OutboxStatus(StrEnum):
    """Состояние транзакционного outbox."""

    PENDING = "pending"
    SENT = "sent"
    FAILED = "failed"
    DEAD = "dead"


class DraftStatus(StrEnum):
    """Состояние черновика сообщения в очереди подтверждения.

    ``SUPERSEDED`` нужен, потому что решение бота устаревает: пока рекрутер
    смотрел черновик, кандидат мог ответить, и новый шаг воронки породил новый
    черновик. Отправлять старый после этого нельзя — кандидат получил бы ответ
    не на то сообщение.
    """

    PENDING = "pending"
    APPROVED = "approved"
    REJECTED = "rejected"
    SUPERSEDED = "superseded"


class ProfileStatus(StrEnum):
    """Состояние профиля скоринга.

    ``SHADOW`` — режим пилота: система считает и показывает результат, но ничего
    не отправляет кандидату. Именно с него начинается запуск на реальных людях:
    сначала сравниваем решения бота с решениями человека, потом включаем.
    """

    DRAFT = "draft"
    SHADOW = "shadow"
    ACTIVE = "active"
    ARCHIVED = "archived"


class ThresholdMode(StrEnum):
    """Как применяются пороги.

    ``ABSOLUTE`` — по проценту. ``RELATIVE`` — по месту среди откликнувшихся
    (топ-K), что нужно при массовом подборе, когда все кандидаты сильные
    или все слабые.
    """

    ABSOLUTE = "absolute"
    RELATIVE = "relative"


class QuestionKind(StrEnum):
    """Тип вопроса — влияет на формулировку и на разбор ответа."""

    FACT = "fact"
    PREFERENCE = "preference"
    CONDITION = "condition"
    EXPERIENCE = "experience"


class DecisionKind(StrEnum):
    """Итоговое решение после скоринга и вопросов."""

    INVITE = "invite"
    HUMAN = "human"
    REJECT = "reject"
    NO_DATA = "no_data"


class StopReason(StrEnum):
    """Почему бот перестал задавать вопросы.

    Нужен для объяснимости: рекрутер видит не только «спросили 2», но и почему
    не спросили остальные три.
    """

    LIMIT_REACHED = "limit_reached"
    DECISION_STABLE = "decision_stable"
    BLOCKER_FOUND = "blocker_found"
    CONTRADICTION = "contradiction"
    UNCLEAR_TWICE = "unclear_twice"
    CANDIDATE_ASKED = "candidate_asked"
    TIMEOUT = "timeout"
    NO_QUESTIONS = "no_questions"
