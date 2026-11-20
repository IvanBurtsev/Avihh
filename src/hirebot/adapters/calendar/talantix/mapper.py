"""Отображение GraphQL-ответов Talantix в DTO/доменные сущности.

Особенности, которые определяют код:

* скаляр ``Instant`` — это epoch в **миллисекундах**, а не строка;
* бизнес-ошибки приходят внутри ``data`` как объекты ``*Error`` с полем
  ``errorType`` (``ACCESS_DENIED``, ``VALIDATION``, ``INTEGRATION_ERROR``…);
* календарная встреча не содержит «свободно/занято» — только интервалы, поэтому
  ``BusyInterval`` строится из пары ``startDate``/``endDate``.

Рабочих часов и графика менеджера в API нет: сетку слотов считает адаптер
(см. ``adapter.find_free_slots``), а не площадка.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from hirebot.adapters.parsing import (
    as_dict,
    as_list,
    dig,
    int_or_none,
    parse_datetime,
    str_or_empty,
)
from hirebot.domain.entities import BusyInterval
from hirebot.domain.errors import ChannelUnavailable, ConfigError, NotFound
from hirebot.logging_setup import get_logger

__all__ = [
    "TalantixManager",
    "TalantixMeeting",
    "build_meeting_input",
    "parse_managers_page",
    "parse_meeting",
    "parse_meeting_node",
    "parse_meetings_node",
    "raise_business_error",
    "to_local_iso",
    "to_millis",
]

logger = get_logger(__name__)

#: Типы бизнес-ошибок Talantix, означающие проблему прав/тарифа, а не наш запрос.
_ACCESS_ERRORS = frozenset({"ACCESS_DENIED", "SEND_SMS_ACCESS_DENIED"})

#: Типы бизнес-ошибок, означающие отказ внешней системы (нужен повтор/внимание).
_EXTERNAL_ERRORS = frozenset({"INTEGRATION_ERROR", "EMAIL_SERVER_NOT_AVAILABLE"})

#: Типы бизнес-ошибок «сущность не найдена».
_NOT_FOUND_ERRORS = frozenset({"NOT_FOUND"})


def to_millis(value: datetime) -> int:
    """``datetime`` (UTC) -> epoch в миллисекундах (скаляр ``Instant``)."""
    return int(value.timestamp() * 1000)


def to_local_iso(value: datetime, timezone: str) -> str:
    """Локальная строка ISO-8601 **без смещения** + IANA-зона.

    ``LocalizedPeriodInput`` принимает именно такое представление: сервер сам
    переводит локальное время в UTC по ``timezoneId``. Если отправить время со
    смещением, есть риск двойной конвертации.
    """
    local = value.astimezone(ZoneInfo(timezone))
    return local.replace(tzinfo=None).isoformat(timespec="seconds")


@dataclass(slots=True, frozen=True)
class TalantixManager:
    """Менеджер компании (``CompanyManager``)."""

    external_id: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    blocked: bool = False
    role: str = ""

    @property
    def full_name(self) -> str:
        return " ".join(part for part in (self.first_name, self.last_name) if part)


@dataclass(slots=True, frozen=True)
class TalantixMeeting:
    """Встреча в календаре Talantix.

    Именно этот объект возвращают методы календаря: ``external_id`` — то, что
    сохраняется в ``bookings.external_meeting_id``.
    """

    external_id: str
    start: datetime
    end: datetime
    topic: str = ""
    place: str = ""
    comment: str = ""
    timezone: str = ""
    manager_ids: tuple[str, ...] = ()
    can_edit: bool = False
    external_event_id: str | None = None
    raw: dict[str, Any] = field(default_factory=dict)

    def to_busy(self) -> BusyInterval:
        return BusyInterval(
            start=self.start,
            end=self.end,
            manager_id=self.manager_ids[0] if self.manager_ids else None,
            title=self.topic or None,
        )


def raise_business_error(error_type: str, message: str, *, context: str) -> None:
    """Бизнес-ошибка Talantix -> доменная ошибка.

    ``errorType`` разбирается, а не ``message``: текст сообщения может меняться
    и локализоваться, а тип ошибки — часть контракта.
    """
    kind = error_type.strip().upper()
    detail = f"{context}: {kind or 'ERROR'} — {message or 'без описания'}"
    if kind in _ACCESS_ERRORS:
        msg = (
            f"{detail}. Проверьте тариф Talantix и права владельца токена: "
            "для создания встреч нужен оплаченный тариф и рабочее место администратора"
        )
        raise ConfigError(msg)
    if kind in _NOT_FOUND_ERRORS:
        raise NotFound(detail)
    if kind in _EXTERNAL_ERRORS:
        raise ChannelUnavailable(detail)
    if kind == "VALIDATION":
        raise ConfigError(f"{detail}. Данные запроса не прошли валидацию на стороне Talantix")
    raise ChannelUnavailable(detail)


def parse_meeting(raw: Mapping[str, Any]) -> TalantixMeeting | None:
    """``CalendarMeetingItem`` -> ``TalantixMeeting`` (``None``, если нет id/дат)."""
    meeting_id = int_or_none(raw.get("id"))
    start = _instant(raw.get("startDate"))
    end = _instant(raw.get("endDate"))
    if meeting_id is None or start is None or end is None:
        return None
    managers: list[str] = []
    for raw_manager in as_list(raw.get("managers")):
        manager_id = int_or_none(as_dict(raw_manager).get("id"))
        if manager_id is not None:
            managers.append(str(manager_id))
    external = as_dict(raw.get("externalCalendarMeeting"))
    return TalantixMeeting(
        external_id=str(meeting_id),
        start=start,
        end=end,
        topic=str_or_empty(raw.get("topic")),
        place=str_or_empty(raw.get("place")),
        comment=str_or_empty(raw.get("comment")),
        timezone=str_or_empty(dig(raw, "timezone", "id")) or str_or_empty(dig(raw, "timezone", "name")),
        manager_ids=tuple(managers),
        can_edit=bool(raw.get("canEdit")),
        external_event_id=str_or_empty(external.get("eventId")) or None,
        raw=dict(raw),
    )


def _instant(value: object) -> datetime | None:
    """Разобрать ``Instant``: миллисекунды epoch, реже — ISO-строка."""
    return parse_datetime(value)


def parse_meeting_node(node: Mapping[str, Any], *, context: str) -> TalantixMeeting | None:
    """Разобрать union-ответ с одной встречей (``calendarMeeting``, ``create…``).

    Возвращает ``None``, если площадка вернула ветку ошибки с ``NOT_FOUND`` —
    вызывающая сторона решает, что это значит (для верификации — «встречи нет»).
    """
    typename = str_or_empty(node.get("__typename"))
    if typename.endswith("Error"):
        error_type = str_or_empty(node.get("errorType"))
        message = str_or_empty(node.get("message"))
        if error_type.strip().upper() in _NOT_FOUND_ERRORS:
            return None
        raise_business_error(error_type, message, context=context)
    meeting = parse_meeting(node)
    if meeting is None:
        msg = f"{context}: Talantix вернул неполную встречу (нет id или дат)"
        raise ChannelUnavailable(msg)
    return meeting


def parse_meetings_node(node: Mapping[str, Any], *, context: str) -> list[TalantixMeeting]:
    """Разобрать union ``CalendarMeetings`` (список встреч за период)."""
    typename = str_or_empty(node.get("__typename"))
    if typename.endswith("Error"):
        raise_business_error(
            str_or_empty(node.get("errorType")),
            str_or_empty(node.get("message")),
            context=context,
        )
    meetings: list[TalantixMeeting] = []
    for raw_item in as_list(node.get("items")):
        meeting = parse_meeting(as_dict(raw_item))
        if meeting is None:
            logger.warning("talantix_meeting_skipped", reason="неполные поля встречи")
            continue
        meetings.append(meeting)
    return meetings


def parse_managers_page(
    node: Mapping[str, Any], *, context: str
) -> tuple[list[TalantixManager], str | None, bool]:
    """Разобрать страницу ``managers``: (элементы, курсор, есть ли следующая).

    Курсорная пагинация описана в ``docs/guides/graphQL/pagination``:
    берём ``pageInfo.endCursor`` и повторяем, пока ``hasNextPage``.
    """
    typename = str_or_empty(node.get("__typename"))
    if typename.endswith("Error"):
        raise_business_error(
            str_or_empty(node.get("errorType")),
            str_or_empty(node.get("message")),
            context=context,
        )
    managers: list[TalantixManager] = []
    for raw_item in as_list(node.get("items")):
        item = as_dict(raw_item)
        manager_id = int_or_none(item.get("id"))
        if manager_id is None:
            continue
        managers.append(
            TalantixManager(
                external_id=str(manager_id),
                first_name=str_or_empty(item.get("firstName")),
                last_name=str_or_empty(item.get("lastName")),
                email=str_or_empty(item.get("email")),
                blocked=bool(item.get("blocked")),
                role=str_or_empty(item.get("managerRole")),
            )
        )
    page_info = as_dict(node.get("pageInfo"))
    cursor = str_or_empty(page_info.get("endCursor")) or None
    has_next = bool(page_info.get("hasNextPage"))
    return managers, cursor, has_next


def build_meeting_input(
    *,
    title: str,
    start: datetime,
    end: datetime,
    timezone: str,
    manager_ids: list[int],
    description: str = "",
    candidate_email: str | None = None,
    external_calendar_type: str | None = None,
    send_email_to_managers: bool = True,
) -> dict[str, Any]:
    """Собрать ``CalendarMeetingCreateInput``.

    Лимиты полей (из схемы): ``topic``/``place`` ≤ 512, ``comment`` ≤ 20 000,
    ``personEmail.text`` ≤ 50 000, ``managerIds`` ≤ 20. Обрезаем осознанно и
    сообщаем в лог, а не отправляем запрос, который гарантированно упадёт.
    """
    if len(manager_ids) > 20:
        msg = f"Talantix принимает не больше 20 участников встречи, передано {len(manager_ids)}"
        raise ConfigError(msg)
    payload: dict[str, Any] = {
        "period": {
            "start": to_local_iso(start, timezone),
            "end": to_local_iso(end, timezone),
            "timezoneId": timezone,
        },
        "managerIds": manager_ids,
        "topic": _trim(title, 512, name="topic"),
        "sendEmailToManagers": send_email_to_managers,
    }
    if description:
        payload["comment"] = _trim(_as_html(description), 20_000, name="comment")
    if candidate_email:
        payload["personEmail"] = {"text": _as_html(description or title)}
    if external_calendar_type:
        payload["externalCalendarType"] = external_calendar_type
    return payload


def _as_html(text: str) -> str:
    """Talantix ожидает HTML в ``comment``/``personEmail.text``.

    Экранируем, потому что текст приходит из шаблонов сообщений: незакрытый тег
    или ``<script>`` в письме кандидату — это и порча письма, и XSS в интерфейсе ATS.
    """
    escaped = text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return escaped.replace("\r\n", "\n").replace("\n", "<br>")


def _trim(value: str, limit: int, *, name: str) -> str:
    if len(value) <= limit:
        return value
    logger.warning("talantix_field_truncated", field=name, limit=limit, length=len(value))
    return value[:limit]
