"""Обобщённое чтение результатов сервисов.

Задачи воркера и API получают результаты чужих для слоя доставки типов
(``PollResult``, ``IngestOutcome``, ``ConversationOutcome``). Чтобы не тащить их
в контракт очереди, результаты читаются обобщённо:

* счётчики — только ``int``/``float`` и длины коллекций (числа безопасно писать
  в лог и отдавать в API);
* состояние диалога — по нескольким возможным именам атрибута.

Никакие строковые поля в результат не попадают: в них могут быть имена,
телефоны и адреса кандидатов.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from hirebot.domain.enums import ConversationState

#: Имена, под которыми результат может отдавать состояние диалога.
_STATE_ATTRIBUTES = ("state", "conversation_state", "new_state")


def as_mapping(value: object) -> dict[str, Any]:
    """Привести результат к словарю: dataclass, pydantic-модель или объект."""
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    if dataclasses.is_dataclass(value) and not isinstance(value, type):
        return {field.name: getattr(value, field.name) for field in dataclasses.fields(value)}
    dump = getattr(value, "model_dump", None)
    if callable(dump):
        data: dict[str, Any] = dict(dump())
        return data
    if hasattr(value, "__dict__"):
        return {key: item for key, item in vars(value).items() if not key.startswith("_")}
    return {}


def int_counters(value: object) -> dict[str, int]:
    """Выбрать из результата числовые счётчики.

    Логические значения отбрасываются намеренно: ``True`` — это флаг, а не
    количество, и в отчёте он только путает.
    """
    counters: dict[str, int] = {}
    for key, item in as_mapping(value).items():
        if key.startswith("_") or isinstance(item, bool):
            continue
        if isinstance(item, int):
            counters[key] = item
        elif isinstance(item, float) and item.is_integer():
            counters[key] = int(item)
        elif isinstance(item, list | tuple | set | frozenset):
            counters[f"{key}_count"] = len(item)
    return counters


def conversation_state(value: object) -> ConversationState | None:
    """Достать состояние диалога из результата сервиса, если оно там есть."""
    for attribute in _STATE_ATTRIBUTES:
        if not hasattr(value, attribute):
            continue
        raw = getattr(value, attribute)
        if isinstance(raw, ConversationState):
            return raw
        if isinstance(raw, str):
            try:
                return ConversationState(raw)
            except ValueError:
                continue
    return None


__all__ = ["as_mapping", "conversation_state", "int_counters"]
