"""Адаптер календаря Talantix (GraphQL ``calendarMeetings`` / ``createCalendarMeeting``).

Главная идея: **Talantix — источник истины по встрече, наш сервис — по брони**.
Free/busy-эндпоинта у Talantix нет, рабочих часов менеджеров в API тоже нет,
поэтому сетку слотов строит адаптер:

* окно поиска — в UTC, максимум 93 дня (это лимит самого API);
* рабочее окно по умолчанию 10:00–19:00 **в зоне менеджера** (конфигурируемо);
* между встречами вычитается буфер (по умолчанию 10 минут) — переезд между
  комнатами и заметки после интервью;
* слоты в прошлом не предлагаются, сравнение — через ``ClockPort``
  (иначе тесты и повторная обработка ведут себя недетерминированно);
* предлагается не больше ``limit`` вариантов, распределённых по разным дням:
  три слота подряд в один день — плохое предложение, у кандидата должен быть выбор.

Про «свободно»: пустой интервал в Talantix **не означает**, что человек свободен
(отпуск, личная встреча, больничный вне системы). Поэтому сервис обязан писать
кандидату «конфликта в календаре нет», а не «свободно», и всегда предлагать выбор.
"""

from __future__ import annotations

from collections.abc import Iterator, Sequence
from datetime import UTC, date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from hirebot.adapters.calendar.talantix.client import TalantixClient
from hirebot.adapters.calendar.talantix.mapper import (
    TalantixManager,
    TalantixMeeting,
    build_meeting_input,
    parse_meeting_node,
    parse_meetings_node,
    raise_business_error,
    to_millis,
)
from hirebot.adapters.calendar.talantix.queries import (
    CALENDAR_MEETING_QUERY,
    CALENDAR_MEETINGS_QUERY,
    CREATE_CALENDAR_MEETING_MUTATION,
    DELETE_CALENDAR_MEETING_MUTATION,
)
from hirebot.adapters.parsing import as_dict, int_or_none, str_or_empty
from hirebot.config import TalantixSettings
from hirebot.domain.entities import BusyInterval, InterviewSlot
from hirebot.domain.errors import ChannelUnavailable, ConfigError, DomainError
from hirebot.logging_setup import get_logger
from hirebot.ports.channels import CalendarPort
from hirebot.ports.infra import CachePort, ClockPort, HttpClientPort, LockPort, NullCache

__all__ = ["TalantixCalendarAdapter"]

logger = get_logger(__name__)

#: Жёсткий лимит Talantix: запрос шире 93 дней отклоняется.
_MAX_WINDOW_DAYS = 93
#: Маркер идемпотентности в комментарии встречи (см. раздел 3.3 ARCHITECTURE.md).
_MARKER_TEMPLATE = "[hirebot:{key}]"


class TalantixCalendarAdapter(CalendarPort):
    """Календарь собеседований Talantix."""

    def __init__(
        self,
        settings: TalantixSettings,
        *,
        http: HttpClientPort,
        clock: ClockPort,
        cache: CachePort | None = None,
        locks: LockPort | None = None,
        cipher: object | None = None,
        buffer_minutes: int = 10,
        workday_start_hour: int = 10,
        workday_end_hour: int = 19,
        slot_step_minutes: int = 30,
        min_lead_minutes: int = 0,
        work_timezone: str | None = None,
        external_calendar_type: str | None = None,
    ) -> None:
        self._settings = settings
        self._clock = clock
        self._client = TalantixClient(
            settings,
            http=http,
            clock=clock,
            cache=cache if cache is not None else NullCache(),
            locks=locks,
            cipher=cipher,
        )
        self._buffer_minutes = max(0, buffer_minutes)
        self._workday_start_hour = workday_start_hour
        self._workday_end_hour = workday_end_hour
        self._slot_step_minutes = max(5, slot_step_minutes)
        self._min_lead_minutes = max(0, min_lead_minutes)
        self._work_timezone_name = work_timezone or settings.default_timezone
        self._external_calendar_type = external_calendar_type

    @property
    def client(self) -> TalantixClient:
        return self._client

    # ------------------------------------------------------------ служебное

    def _zone(self) -> ZoneInfo:
        try:
            return ZoneInfo(self._work_timezone_name)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            msg = (
                f"Неизвестная зона рабочего окна {self._work_timezone_name!r}. "
                "Задайте HIREBOT_TALANTIX__DEFAULT_TIMEZONE (IANA, например Europe/Moscow)"
            )
            raise ConfigError(msg) from exc

    def _validate_window(self, start: datetime, end: datetime) -> None:
        if end <= start:
            msg = f"Некорректное окно: end ({end.isoformat()}) не позже start ({start.isoformat()})"
            raise ConfigError(msg)
        limit_days = min(self._settings.max_window_days, _MAX_WINDOW_DAYS)
        if end - start > timedelta(days=limit_days):
            msg = (
                f"Окно запроса {start.isoformat()}..{end.isoformat()} шире допустимых "
                f"{limit_days} дней: Talantix отклоняет запрос шире 93 дней "
                "(HIREBOT_TALANTIX__MAX_WINDOW_DAYS). Нарежьте диапазон на части"
            )
            raise ConfigError(msg)

    async def healthcheck(self) -> bool:
        """Проверить токен через ``GET /auth_check`` (успех — 204)."""
        try:
            return await self._client.auth_check()
        except DomainError as exc:
            logger.warning("talantix_healthcheck_failed", error=str(exc))
            return False

    async def list_managers(self, *, first: int = 50) -> list[TalantixManager]:
        """Менеджеры компании: в настройках ID заданы строками, API принимает Int."""
        return await self._client.list_managers(first=first)

    # ------------------------------------------------------------ занятость

    async def list_busy(
        self, *, manager_ids: Sequence[str], start: datetime, end: datetime
    ) -> Sequence[BusyInterval]:
        """Занятые интервалы участников в окне [start, end).

        ``calendarMeetings`` возвращает встречи, а не «занятость»: интервалы
        строим сами. Пагинации у этого корня нет — только диапазон дат.
        """
        meetings = await self._fetch_meetings(start, end)
        wanted = {str(manager_id) for manager_id in manager_ids if str(manager_id)}
        intervals: list[BusyInterval] = []
        for meeting in meetings:
            if wanted and not (set(meeting.manager_ids) & wanted):
                continue
            intervals.append(meeting.to_busy())
        return intervals

    async def _fetch_meetings(self, start: datetime, end: datetime) -> list[TalantixMeeting]:
        self._validate_window(start, end)
        data = await self._client.graphql(
            CALENDAR_MEETINGS_QUERY,
            {"start": to_millis(start), "end": to_millis(end)},
        )
        return parse_meetings_node(as_dict(data.get("calendarMeetings")), context="calendarMeetings")

    # ------------------------------------------------------------ слоты

    async def find_free_slots(
        self,
        *,
        manager_ids: Sequence[str],
        start: datetime,
        end: datetime,
        duration_minutes: int,
        limit: int = 3,
    ) -> Sequence[InterviewSlot]:
        """Подобрать слоты с учётом занятости, буферов и рабочих часов.

        Если участников несколько, слот считается свободным только когда свободны
        **все**: интервью с группой интервьюеров не должно ломаться о чужую встречу.
        В ``InterviewSlot.manager_id`` попадает первый участник — этого достаточно
        для брони, а фактический список участников уходит в ``create_meeting``.
        """
        if duration_minutes <= 0:
            msg = f"Длительность собеседования должна быть положительной: {duration_minutes}"
            raise ConfigError(msg)
        if limit <= 0:
            return []
        self._validate_window(start, end)

        zone = self._zone()
        duration = timedelta(minutes=duration_minutes)
        buffer = timedelta(minutes=self._buffer_minutes)
        now = self._clock.now()
        earliest = now + timedelta(minutes=self._min_lead_minutes)

        busy = await self._fetch_meetings(start, end)
        wanted = {str(manager_id) for manager_id in manager_ids if str(manager_id)}
        blocked: list[tuple[datetime, datetime]] = [
            (meeting.start - buffer, meeting.end + buffer)
            for meeting in busy
            if not wanted or (set(meeting.manager_ids) & wanted)
        ]

        manager_id = str(manager_ids[0]) if manager_ids else ""
        per_day: dict[date, list[InterviewSlot]] = {}
        for day in _local_days(start, end, zone):
            slots: list[InterviewSlot] = []
            for slot_start in _day_candidates(
                day,
                zone,
                duration,
                start_hour=self._workday_start_hour,
                end_hour=self._workday_end_hour,
                step_minutes=self._slot_step_minutes,
            ):
                slot_end = slot_start + duration
                if slot_start < start or slot_end > end:
                    continue
                # «В прошлом» — строго по часам: наивная проверка даты пропускает
                # слот, который начался минуту назад.
                if slot_start <= now or slot_start < earliest:
                    continue
                if any(
                    slot_start < blocked_end and blocked_start < slot_end
                    for blocked_start, blocked_end in blocked
                ):
                    continue
                slots.append(
                    InterviewSlot(
                        start=slot_start,
                        end=slot_end,
                        manager_id=manager_id,
                        timezone=self._work_timezone_name,
                    )
                )
            if slots:
                per_day[day] = slots
        return _spread_across_days(per_day, limit)

    # ------------------------------------------------------------ запись

    async def create_meeting(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        timezone: str,
        manager_ids: Sequence[str],
        candidate_email: str | None = None,
        description: str = "",
        idempotency_key: str = "",
    ) -> TalantixMeeting:
        """Создать встречу.

        Идемпотентность: маркер ``[hirebot:<key>]`` добавляется в ``comment``,
        и перед созданием мы ищем встречу с этим маркером. Так повтор после
        таймаута не создаёт вторую встречу (верификация — ``get_meeting``).
        """
        self._validate_window(start, end)
        manager_ids_int = _to_int_ids(manager_ids, field="manager_ids")
        if idempotency_key:
            existing = await self._find_by_marker(start, end, idempotency_key)
            if existing is not None:
                logger.info("talantix_meeting_already_created", meeting_id=existing.external_id)
                return existing

        comment = self._compose_comment(description, idempotency_key)
        payload = build_meeting_input(
            title=title,
            start=start,
            end=end,
            timezone=timezone or self._work_timezone_name,
            manager_ids=manager_ids_int,
            description=comment,
            candidate_email=candidate_email,
            external_calendar_type=self._external_calendar_type,
        )
        data = await self._client.graphql(CREATE_CALENDAR_MEETING_MUTATION, {"input": payload}, mutation=True)
        meeting = parse_meeting_node(
            as_dict(data.get("createCalendarMeeting")), context="createCalendarMeeting"
        )
        if meeting is None:  # pragma: no cover - защита от ветки NOT_FOUND у мутации
            msg = "Talantix не вернул созданную встречу"
            raise ChannelUnavailable(msg)
        logger.info(
            "talantix_meeting_created",
            meeting_id=meeting.external_id,
            idempotency_key=idempotency_key or None,
            external_event_id=meeting.external_event_id,
        )
        return meeting

    async def cancel_meeting(self, external_id: str) -> None:
        """Удалить встречу (``deleteCalendarMeeting``)."""
        meeting_id = int_or_none(external_id)
        if meeting_id is None:
            msg = f"Некорректный идентификатор встречи Talantix: {external_id!r}"
            raise ConfigError(msg)
        data = await self._client.graphql(DELETE_CALENDAR_MEETING_MUTATION, {"id": meeting_id}, mutation=True)
        node = as_dict(data.get("deleteCalendarMeeting"))
        typename = str_or_empty(node.get("__typename"))
        if typename.endswith("Error"):
            raise_business_error(
                str_or_empty(node.get("errorType")),
                str_or_empty(node.get("message")),
                context="deleteCalendarMeeting",
            )
        logger.info("talantix_meeting_cancelled", meeting_id=external_id)

    async def get_meeting(self, external_id: str) -> TalantixMeeting | None:
        """Перечитать встречу — обязательный шаг верификации после создания."""
        meeting_id = int_or_none(external_id)
        if meeting_id is None:
            msg = f"Некорректный идентификатор встречи Talantix: {external_id!r}"
            raise ConfigError(msg)
        data = await self._client.graphql(CALENDAR_MEETING_QUERY, {"id": meeting_id})
        return parse_meeting_node(as_dict(data.get("calendarMeeting")), context="calendarMeeting")

    # ------------------------------------------------------------ внутреннее

    def _compose_comment(self, description: str, idempotency_key: str) -> str:
        marker = _MARKER_TEMPLATE.format(key=idempotency_key) if idempotency_key else ""
        return "\n".join(part for part in (description, marker) if part)

    async def _find_by_marker(
        self, start: datetime, end: datetime, idempotency_key: str
    ) -> TalantixMeeting | None:
        """Найти уже созданную встречу по маркеру идемпотентности.

        Окно поиска расширяем на сутки в обе стороны: встреча могла быть создана
        при предыдущей попытке с небольшим сдвигом времени.
        """
        marker = _MARKER_TEMPLATE.format(key=idempotency_key)
        limit = timedelta(days=min(self._settings.max_window_days, _MAX_WINDOW_DAYS))
        window_start = start - timedelta(days=1)
        window_end = end + timedelta(days=1)
        if window_end - window_start > limit:
            # Расширенное окно выходит за лимит API: ищем в исходном.
            window_start, window_end = start, end
        try:
            meetings = await self._fetch_meetings(window_start, window_end)
        except DomainError as exc:
            logger.warning("talantix_marker_lookup_failed", error=str(exc))
            return None
        for meeting in meetings:
            if marker in meeting.comment:
                return meeting
        return None


def _to_int_ids(values: Sequence[str], *, field: str) -> list[int]:
    """Преобразовать строковые ID в Int (API принимает только числа)."""
    result: list[int] = []
    for value in values:
        parsed = int_or_none(value)
        if parsed is None:
            msg = f"Talantix принимает числовые ID ({field}), получено {value!r}"
            raise ConfigError(msg)
        result.append(parsed)
    return result


def _local_days(start: datetime, end: datetime, zone: ZoneInfo) -> Iterator[date]:
    """Локальные даты, покрывающие окно [start, end] в зоне менеджера."""
    current = start.astimezone(zone).date()
    last = end.astimezone(zone).date()
    guard = 0
    while current <= last and guard <= _MAX_WINDOW_DAYS + 1:
        yield current
        current += timedelta(days=1)
        guard += 1


def _day_candidates(
    day: date,
    zone: ZoneInfo,
    duration: timedelta,
    *,
    start_hour: int,
    end_hour: int,
    step_minutes: int,
) -> Iterator[datetime]:
    """Кандидаты начала слота внутри рабочего окна одного дня (в UTC)."""
    day_start = datetime.combine(day, time(hour=start_hour), tzinfo=zone)
    day_end = datetime.combine(day, time(hour=end_hour), tzinfo=zone)
    step = timedelta(minutes=step_minutes)
    cursor = day_start
    while cursor + duration <= day_end:
        yield cursor.astimezone(UTC)
        cursor += step


def _spread_across_days(per_day: dict[date, list[InterviewSlot]], limit: int) -> list[InterviewSlot]:
    """Взять до ``limit`` слотов, распределив их по разным дням.

    Сначала по одному слоту на каждый день, затем — вторым кругом. Так кандидат
    получает выбор «в разные дни», а не три времени подряд в одном окне.
    """
    result: list[InterviewSlot] = []
    days = sorted(per_day)
    round_index = 0
    while len(result) < limit:
        added = False
        for day in days:
            slots = per_day[day]
            if round_index < len(slots):
                result.append(slots[round_index])
                added = True
                if len(result) >= limit:
                    break
        if not added:
            break
        round_index += 1
    return result
