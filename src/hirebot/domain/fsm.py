"""Конечный автомат воронки.

Зачем отдельный модуль: без него переходы состояний расползаются по сервисам
в виде ``if state == ...``, и появляются баги вида «бот прислал приглашение
кандидату, которого уже отклонили». Здесь переходы описаны декларативно и
проверяются в одном месте.
"""

from __future__ import annotations

from collections.abc import Mapping

from hirebot.domain.enums import ConversationState as S
from hirebot.domain.errors import PolicyViolation

#: Разрешённые переходы. Ключ — текущее состояние, значение — множество целевых.
TRANSITIONS: Mapping[S, frozenset[S]] = {
    S.NEW: frozenset({S.RESUME_FETCHED, S.SCREENED, S.HUMAN_REVIEW, S.REJECTED, S.CLOSED}),
    S.RESUME_FETCHED: frozenset({S.SCREENED, S.HUMAN_REVIEW, S.CLOSED}),
    S.SCREENED: frozenset({S.QUESTIONS_SENT, S.INVITED, S.HUMAN_REVIEW, S.REJECTED, S.CLOSED}),
    S.QUESTIONS_SENT: frozenset({S.AWAITING_ANSWER, S.HUMAN_REVIEW, S.CLOSED}),
    S.AWAITING_ANSWER: frozenset(
        {
            S.SCREENED,  # ответ получен -> пересчёт скоринга
            S.AWAITING_SLOT_CHOICE,
            S.INVITED,
            S.HUMAN_REVIEW,
            S.REJECTED,
            S.CLOSED,
        }
    ),
    S.INVITED: frozenset({S.AWAITING_SLOT_CHOICE, S.BOOKED, S.HUMAN_REVIEW, S.CLOSED, S.REJECTED}),
    # Повторное предложение слотов (self-loop) разрешено: кандидат может
    # попросить другое время, и это нормальный сценарий, а не ошибка.
    S.AWAITING_SLOT_CHOICE: frozenset(
        {S.AWAITING_SLOT_CHOICE, S.BOOKED, S.HUMAN_REVIEW, S.CLOSED, S.REJECTED}
    ),
    S.BOOKED: frozenset({S.AWAITING_SLOT_CHOICE, S.BOOKED, S.CLOSED, S.HUMAN_REVIEW}),
    S.REJECTED: frozenset({S.HUMAN_REVIEW, S.CLOSED}),
    S.HUMAN_REVIEW: frozenset({S.QUESTIONS_SENT, S.INVITED, S.REJECTED, S.SCREENED, S.CLOSED}),
    S.CLOSED: frozenset(),  # терминальное состояние
}

#: Состояния, из которых бот имеет право писать кандидату первым.
OUTBOUND_ALLOWED: frozenset[S] = frozenset(
    {
        S.NEW,
        S.RESUME_FETCHED,
        S.SCREENED,
        S.AWAITING_ANSWER,
        S.INVITED,
        S.AWAITING_SLOT_CHOICE,
        S.BOOKED,
        S.HUMAN_REVIEW,
    }
)

#: Состояния, в которых любое автосообщение запрещено.
NO_OUTBOUND: frozenset[S] = frozenset({S.REJECTED, S.CLOSED})


def can_transition(current: S, target: S) -> bool:
    return target in TRANSITIONS.get(current, frozenset())


def assert_transition(current: S, target: S) -> None:
    """Проверить переход, иначе — ``PolicyViolation``.

    Сознательно исключение, а не ``False``: неверный переход означает ошибку
    в коде вызывающей стороны, и её нужно увидеть в тестах, а не молча проглотить.
    """
    if current == target:
        return
    if not can_transition(current, target):
        msg = f"Недопустимый переход состояния: {current.value} -> {target.value}"
        raise PolicyViolation(msg)


def assert_can_send(current: S) -> None:
    """Проверить право бота отправить сообщение из текущего состояния."""
    if current in NO_OUTBOUND:
        msg = f"Отправка сообщений запрещена в состоянии {current.value}"
        raise PolicyViolation(msg)
    if current not in OUTBOUND_ALLOWED:
        msg = f"Состояние {current.value} не разрешает исходящие сообщения"
        raise PolicyViolation(msg)
