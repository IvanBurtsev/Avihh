"""Сборка исходящих сообщений: ключи идемпотентности и форматирование слотов.

Здесь живут две вещи, которые обязаны быть одинаковыми во всех сервисах:

* **ключ идемпотентности** — формула ``{conversation_id}:{state}:{kind}``
  из архитектуры. Мы добавляем к ней порядковый номер: за один и тот же
  статус диалога может уйти два разных сообщения одного типа (например, две
  порции уточняющих вопросов), и без дискриминатора канал справедливо
  посчитал бы второе сообщение дублем;
* **форматирование слотов** — кандидат видит время в своём часовом поясе,
  а в БД и в календаре всё хранится в UTC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, tzinfo
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hirebot.domain.entities import InterviewSlot, Message
from hirebot.domain.enums import MessageDirection, MessageKind
from hirebot.logging_setup import get_logger

logger = get_logger(__name__)

#: Названия дней недели для человекочитаемого текста (локаль не нужна).
_WEEKDAYS: dict[int, str] = {
    0: "понедельник",
    1: "вторник",
    2: "среда",
    3: "четверг",
    4: "пятница",
    5: "суббота",
    6: "воскресенье",
}

#: Короткие подписи часовых поясов; для остальных показываем IANA-имя.
_TZ_LABELS: dict[str, str] = {"Europe/Moscow": "МСК", "UTC": "UTC"}


def safe_zone(name: str) -> tzinfo:
    """Часовой пояс по IANA-имени; при неизвестном имени — UTC, без падения.

    Неизвестный пояс не должен ломать отправку сообщения: кандидат получит
    время в UTC, а в лог уйдёт предупреждение для разбора.
    """
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError):
        logger.warning("unknown_timezone", timezone=name)
        return UTC


def tz_label(name: str) -> str:
    """Подпись часового пояса для сообщения кандидату."""
    return _TZ_LABELS.get(name, name)


def format_slot_line(index: int, slot: InterviewSlot, *, timezone: str) -> str:
    """Одна строка списка слотов: ``1) понедельник, 01.06.2026 в 15:00 (МСК)``.

    ``index`` — номер, который увидит кандидат (нумерация с единицы): именно
    его он назовёт в ответе, и именно он превратится в ``slot_index - 1``.
    """
    local = slot.start.astimezone(safe_zone(timezone))
    weekday = _WEEKDAYS.get(local.weekday(), "")
    return (
        f"{index}) {weekday}, {local.strftime('%d.%m.%Y')} в {local.strftime('%H:%M')} ({tz_label(timezone)})"
    )


def format_slots(slots: Sequence[InterviewSlot], *, timezone: str) -> list[str]:
    """Пронумерованный список слотов (нумерация с единицы)."""
    return [format_slot_line(i, slot, timezone=timezone) for i, slot in enumerate(slots, start=1)]


def format_slot_moment(slot: InterviewSlot, *, timezone: str) -> str:
    """Один слот без номера — для подтверждения брони."""
    local = slot.start.astimezone(safe_zone(timezone))
    weekday = _WEEKDAYS.get(local.weekday(), "")
    return f"{weekday}, {local.strftime('%d.%m.%Y')} в {local.strftime('%H:%M')} ({tz_label(timezone)})"


def count_outbound_kind(messages: Sequence[Message], kind: MessageKind) -> int:
    """Сколько исходящих сообщений такого типа уже ушло в этом диалоге."""
    return sum(
        1 for message in messages if message.direction is MessageDirection.OUTBOUND and message.kind is kind
    )


def build_idempotency_key(
    conversation_id: UUID,
    kind: MessageKind,
    *,
    ordinal: int = 1,
) -> str:
    """Ключ идемпотентности отправки.

    Ключ обязан зависеть **только от того, что идентифицирует само сообщение**:
    диалог, тип и порядковый номер сообщения этого типа. Состояние диалога в
    ключ не входит намеренно: раньше оно там было, и любое внешнее изменение
    состояния (например ручной возврат диалога в работу) давало новый ключ,
    то есть повторную отправку — кандидат получал два одинаковых сообщения.

    Порядковый номер считается по уже сохранённым исходящим сообщениям этого
    типа. Если сообщение ушло на площадку, но не закоммитилось (обрыв), номер
    не изменится, ключ совпадёт с прошлым, и площадка отбросит дубль.
    """
    base = f"{conversation_id}:{kind.value}"
    return base if ordinal <= 1 else f"{base}:{ordinal}"
