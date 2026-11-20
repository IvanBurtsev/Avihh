"""Контракт ``CalendarPort``: набор, который обязан пройти любой календарь.

Порт намеренно узкий (ARCHITECTURE.md, п. 3.3): свободных слотов площадка не
отдаёт, поэтому сетку строит адаптер, а сервис получает готовые слоты. Отсюда
набор требований:

* подбор слотов уважает занятость, окно, длительность и ``limit``;
* в прошлом слотов нет — предлагать прошедшее время нельзя;
* ``create_meeting`` возвращает объект с непустым ``external_id``: без него
  невозможна обязательная верификация перечитыванием;
* созданная встреча читается ``get_meeting``, отменённая — уже нет.

Ассерты идут только по методам порта; данные готовит подготовка двойника.
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from conformance_support import (
    DURATION_MINUTES,
    CalendarHarness,
    find_slots,
)

from hirebot.domain.entities import InterviewSlot
from hirebot.ports.channels import CalendarPort
from hirebot.services.scheduling import meeting_external_id

#: Все методы ``CalendarPort``: каждый обязан быть корутиной.
CALENDAR_METHODS: tuple[str, ...] = (
    "list_busy",
    "find_free_slots",
    "create_meeting",
    "cancel_meeting",
    "get_meeting",
)

TIMEZONE = "Europe/Moscow"


def test_calendar_satisfies_calendar_port_protocol(calendar_harness: CalendarHarness) -> None:
    """Реализация структурно соответствует ``CalendarPort``."""
    assert isinstance(calendar_harness.port, CalendarPort)


@pytest.mark.parametrize("method_name", CALENDAR_METHODS)
def test_calendar_methods_are_coroutines(calendar_harness: CalendarHarness, method_name: str) -> None:
    """Все методы порта асинхронны."""
    method = getattr(calendar_harness.port, method_name)
    assert inspect.iscoroutinefunction(method)


async def test_list_busy_reports_added_interval(calendar_harness: CalendarHarness) -> None:
    """Занятость, добавленная в окне, видна через ``list_busy``."""
    busy_start = calendar_harness.start + timedelta(hours=1)
    busy_end = busy_start + timedelta(minutes=DURATION_MINUTES)
    calendar_harness.add_busy(busy_start, busy_end, manager_id=calendar_harness.manager_ids[0])

    intervals = await calendar_harness.port.list_busy(
        manager_ids=list(calendar_harness.manager_ids),
        start=calendar_harness.start,
        end=calendar_harness.end,
    )

    assert any(interval.start == busy_start and interval.end == busy_end for interval in intervals)


async def test_free_slots_do_not_overlap_busy_intervals(calendar_harness: CalendarHarness) -> None:
    """Слот, пересекающийся с занятостью, не предлагается (защита от двойной брони)."""
    slots = await find_slots(calendar_harness, limit=3)
    assert slots, "календарь не предложил ни одного слота: проверять нечего"
    taken = slots[0]
    calendar_harness.add_busy(taken.start, taken.end, manager_id=taken.manager_id)

    after = await find_slots(calendar_harness, limit=3)

    assert after, "после добавления занятости свободное время ещё есть"
    assert not any(slot.overlaps(taken.start, taken.end) for slot in after)


async def test_free_slots_stay_inside_requested_window(calendar_harness: CalendarHarness) -> None:
    """Слоты не выходят за переданное окно [start, end)."""
    slots = await find_slots(calendar_harness, limit=3)

    assert slots
    assert all(calendar_harness.start <= slot.start and slot.end <= calendar_harness.end for slot in slots)


async def test_free_slots_respect_duration(calendar_harness: CalendarHarness) -> None:
    """Длительность слота равна запрошенной, а не «как получится»."""
    slots = await find_slots(calendar_harness, limit=2, duration_minutes=30)

    assert slots
    assert all(slot.duration == timedelta(minutes=30) for slot in slots)


@pytest.mark.parametrize("limit", [1, 2, 3])
async def test_free_slots_respect_limit(calendar_harness: CalendarHarness, limit: int) -> None:
    """``limit`` — жёсткая граница: предлагать больше нельзя."""
    slots = await find_slots(calendar_harness, limit=limit)

    assert 1 <= len(slots) <= limit


async def test_free_slots_are_never_in_the_past(calendar_harness: CalendarHarness) -> None:
    """Прошедшее время предлагать нельзя: кандидат не может выбрать «вчера»."""
    slots = await find_slots(
        calendar_harness,
        limit=5,
        start=calendar_harness.now - timedelta(days=3),
        end=calendar_harness.now + timedelta(days=1),
    )

    assert slots, "пустой список не доказывает, что фильтр прошлого работает"
    assert all(slot.start >= calendar_harness.now for slot in slots)


async def test_create_meeting_returns_object_with_external_id(calendar_harness: CalendarHarness) -> None:
    """Создание встречи возвращает объект с непустым ``external_id``."""
    slots = await find_slots(calendar_harness, limit=1)
    target = slots[0]

    meeting = await create_meeting(calendar_harness, target)

    assert meeting_external_id(meeting), "без external_id верификация перечитыванием невозможна"


async def test_created_meeting_is_readable_and_cancelled_meeting_is_not(
    calendar_harness: CalendarHarness,
) -> None:
    """``get_meeting`` находит созданную встречу, а после отмены — не находит."""
    slots = await find_slots(calendar_harness, limit=1)
    meeting = await create_meeting(calendar_harness, slots[0])
    external_id = meeting_external_id(meeting)
    assert external_id is not None

    assert await calendar_harness.port.get_meeting(external_id) is not None

    await calendar_harness.port.cancel_meeting(external_id)

    assert await calendar_harness.port.get_meeting(external_id) is None


async def test_create_meeting_with_same_idempotency_key_does_not_create_second_meeting(
    calendar_harness: CalendarHarness,
) -> None:
    """Повтор создания с тем же ``idempotency_key`` возвращает ту же встречу.

    На этом держится восстановление после таймаута (ARCHITECTURE.md, п. 6.3):
    ответ мог потеряться, но встреча уже создана.
    """
    slots = await find_slots(calendar_harness, limit=1)
    target = slots[0]

    first = await create_meeting(calendar_harness, target, idempotency_key="booking-1")
    repeated = await create_meeting(calendar_harness, target, idempotency_key="booking-1")

    assert meeting_external_id(repeated) == meeting_external_id(first)


async def test_cancel_meeting_of_unknown_id_does_not_fail(calendar_harness: CalendarHarness) -> None:
    """Отмена несуществующей встречи идемпотентна: это best-effort шаг сервиса."""
    await calendar_harness.port.cancel_meeting("conformance-meeting-does-not-exist")


async def test_time_of_created_meeting_is_not_offered_again(calendar_harness: CalendarHarness) -> None:
    """Созданная встреча — это занятость: предлагать её время снова нельзя."""
    slots = await find_slots(calendar_harness, limit=3)
    assert slots
    target = slots[0]

    await create_meeting(calendar_harness, target)

    after = await find_slots(calendar_harness, limit=3)

    assert not any(slot.overlaps(target.start, target.end) for slot in after)


async def create_meeting(
    calendar_harness: CalendarHarness,
    slot: InterviewSlot,
    *,
    idempotency_key: str = "",
) -> object:
    """Создать встречу в контрактных данных (общий шаг нескольких тестов)."""
    return await calendar_harness.port.create_meeting(
        title="Собеседование: Python-разработчик",
        start=slot.start,
        end=slot.end,
        timezone=TIMEZONE,
        manager_ids=[slot.manager_id],
        candidate_email="candidate@example.com",
        description="Контрактная проверка",
        idempotency_key=idempotency_key,
    )
