"""Планирование цепочки переходов конечного автомата воронки.

Источник истины — ``hirebot.domain.fsm``: набор разрешённых переходов живёт
там и не переписывается. Но воронка иногда требует не одного перехода, а
цепочки: например, приглашение возможно только из ``SCREENED``, а диалог
пришёл в ``NEW``. Раньше это решалось бы «магическим» присваиванием
состояния в обход FSM; здесь мы честно находим путь по разрешённым рёбрам и
проверяем **каждый** шаг через ``fsm.assert_transition``.

Промежуточные состояния не сохраняются: это логические вехи, а не отдельные
транзакции. Значимые вехи (``SCREENED``) сервис фиксирует отдельно.
"""

from __future__ import annotations

from collections import deque

from hirebot.domain.enums import ConversationState as S
from hirebot.domain.errors import PolicyViolation
from hirebot.domain.fsm import TRANSITIONS, assert_transition


def transition_path(current: S, target: S) -> tuple[S, ...]:
    """Кратчайший путь по разрешённым переходам (поиск в ширину).

    Возвращает последовательность состояний **без** ``current``. Для
    ``current == target`` путь пуст. Если пути нет — ``PolicyViolation``:
    молчаливое «телепортирование» состояния запрещено.
    """
    if current == target:
        return ()

    queue: deque[tuple[S, tuple[S, ...]]] = deque([(current, ())])
    visited: set[S] = {current}
    while queue:
        node, path = queue.popleft()
        # Сортировка по значению: обход детерминирован, тесты не «мигают».
        for nxt in sorted(TRANSITIONS.get(node, frozenset()), key=lambda state: state.value):
            if nxt in visited:
                continue
            new_path = (*path, nxt)
            if nxt == target:
                return new_path
            visited.add(nxt)
            queue.append((nxt, new_path))

    msg = f"Не существует допустимого пути {current.value} -> {target.value}"
    raise PolicyViolation(msg)


def apply_transition(current: S, target: S) -> S:
    """Проверить путь и вернуть целевое состояние.

    Каждый шаг проходит через ``fsm.assert_transition`` — единственную точку,
    где описаны правила воронки.
    """
    state = current
    for step in transition_path(current, target):
        assert_transition(state, step)
        state = step
    return state
