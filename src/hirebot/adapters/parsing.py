"""Разбор «сырых» JSON-ответов площадок.

Площадки отдают данные непредсказуемо: поле может быть ``null``, отсутствовать,
прийти строкой вместо числа или «уехать» в другой тип после обновления API.
Все обращения к внешнему JSON идут через эти функции: они никогда не бросают
исключение на неожиданном типе, а возвращают пустое значение. Так одна кривая
строка в ответе не роняет обработку всей пачки откликов.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast

__all__ = [
    "as_dict",
    "as_list",
    "dig",
    "from_millis",
    "int_or_none",
    "int_or_zero",
    "parse_datetime",
    "str_or_empty",
    "str_or_none",
    "to_millis",
]

#: Граница, выше которой число считаем миллисекундами, а не секундами
#: (10^11 с ≈ 5138 год, 10^11 мс ≈ 1973 год).
_MILLIS_THRESHOLD = 1e11


def as_dict(value: object) -> dict[str, Any]:
    """Привести значение к словарю (иначе — пустой словарь)."""
    if isinstance(value, dict):
        return value
    return {}


def as_list(value: object) -> list[Any]:
    """Привести значение к списку (кортеж тоже принимаем, иначе — пустой список)."""
    if isinstance(value, list):
        return value
    if isinstance(value, tuple):
        return list(value)
    return []


def str_or_none(value: object) -> str | None:
    """Строка или ``None``. Числа приводим к строке: идентификаторы приходят и так, и так."""
    if isinstance(value, str):
        text = value.strip()
        return text or None
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return str(value)
    return None


def str_or_empty(value: object) -> str:
    return str_or_none(value) or ""


def int_or_none(value: object) -> int | None:
    """Целое или ``None``. Строки вида ``"42"`` принимаем, ``"abc"`` — нет."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return None


def int_or_zero(value: object) -> int:
    return int_or_none(value) or 0


def dig(obj: object, *keys: str) -> Any:
    """Достать вложенное значение по пути ключей.

    Возвращает ``None``, если любой из промежуточных уровней не словарь или
    ключа нет. Специально ``Any``: вызывающая сторона обязана сама решить,
    какой тип она ожидает (см. ``str_or_none``/``int_or_none``/``as_list``).
    """
    current: Any = obj
    for key in keys:
        if not isinstance(current, dict):
            return None
        current = cast("dict[str, Any]", current).get(key)
    return current


def parse_datetime(value: object) -> datetime | None:
    """Разобрать момент времени в ``datetime`` с зоной UTC.

    Поддерживаются форматы, которые реально встречаются у площадок:

    * ISO-8601 со смещением (``2026-11-20T10:00:00+0300`` — так отдаёт hh.ru);
    * ISO-8601 с ``Z`` (``2026-11-20T07:00:00Z``);
    * epoch в миллисекундах (``1703513801000`` — так устроен скаляр ``Instant`` Talantix);
    * epoch в секундах.

    Наивное значение трактуется как UTC: хранить и сравнивать «наивное локальное
    время сервера» нельзя (см. раздел 2.9 ARCHITECTURE.md).
    """
    if isinstance(value, datetime):
        return value if value.tzinfo is not None else value.replace(tzinfo=UTC)
    if isinstance(value, bool):
        return None
    if isinstance(value, int | float):
        return _from_epoch(float(value))
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        stripped = text.removesuffix("Z").removesuffix("z")
        if stripped.isdigit() or (stripped.startswith("-") and stripped[1:].isdigit()):
            return _from_epoch(float(stripped))
        normalized = text.replace("Z", "+00:00").replace("z", "+00:00")
        try:
            parsed = datetime.fromisoformat(normalized)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None


def _from_epoch(value: float) -> datetime | None:
    seconds = value / 1000.0 if abs(value) > _MILLIS_THRESHOLD else value
    try:
        return datetime.fromtimestamp(seconds, tz=UTC)
    except (OverflowError, OSError, ValueError):
        return None


def to_millis(value: datetime) -> int:
    """Момент времени -> epoch в миллисекундах (формат скаляра ``Instant``)."""
    return int(value.timestamp() * 1000)


def from_millis(value: int) -> datetime:
    return datetime.fromtimestamp(value / 1000.0, tz=UTC)
