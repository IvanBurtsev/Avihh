"""Тесты адаптера календаря Talantix.

Сети нет: GraphQL мокается через ``respx`` на том же уровне, на котором его
вызывает боевой код. Проверяются гарантии, ради которых этот адаптер и написан:

* занятость берётся из ``calendarMeetings`` и связывается с менеджерами;
* слоты не пересекаются с занятостью и не предлагаются в прошлом;
* окно длиннее допустимого (93 дня у Talantix) отклоняется на нашей стороне,
  а не превращается в непонятную ошибку сервера;
* ``errors[]`` различает дефект нашего запроса (``ConfigError``, повтор не нужен)
  и отказ платформы (``ChannelUnavailable``, имеет смысл повторить);
* ключ идемпотентности попадает в тело запроса на создание встречи — именно по
  нему потом ищется «потерявшаяся» встреча при верификации.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

import httpx
import pytest
from adapter_fakes import NOW, make_talantix_adapter

from hirebot.domain.entities import BusyInterval
from hirebot.domain.errors import ChannelUnavailable, ConfigError

GRAPHQL_URL = "https://api.talantix.ru/graphql"

MINUTE = timedelta(minutes=1)
HOUR = timedelta(hours=1)


def _ms(value: datetime) -> int:
    """Talantix принимает ``Instant`` в миллисекундах epoch."""
    return int(value.timestamp() * 1000)


def meeting(
    *,
    meeting_id: int,
    start: datetime,
    end: datetime,
    manager_id: int = 1,
) -> dict[str, Any]:
    return {
        "id": meeting_id,
        "topic": "Собеседование",
        "place": "Онлайн",
        "comment": "",
        "startDate": _ms(start),
        "endDate": _ms(end),
        "canEdit": True,
        "timezone": {"id": 1, "name": "Europe/Moscow"},
        "managers": [
            {"id": manager_id, "firstName": "Иван", "lastName": "Рекрутеров", "email": "r@example.com"}
        ],
        "externalCalendarMeeting": None,
    }


def mock_graphql(respx_mock: Any, *, items: list[dict[str, Any]]) -> Any:
    return respx_mock.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "calendarMeetings": {
                        "__typename": "CalendarMeetingsItem",
                        "items": items,
                    }
                }
            },
        )
    )


async def test_list_busy_maps_meetings_to_intervals(respx_mock: Any) -> None:
    adapter = make_talantix_adapter()
    start = NOW
    end = NOW + timedelta(days=1)
    mock_graphql(
        respx_mock,
        items=[meeting(meeting_id=77, start=NOW + 3 * HOUR, end=NOW + 4 * HOUR)],
    )

    busy = await adapter.list_busy(manager_ids=["1"], start=start, end=end)

    assert len(busy) == 1
    interval = busy[0]
    assert isinstance(interval, BusyInterval)
    assert interval.start == NOW + 3 * HOUR
    assert interval.end == NOW + 4 * HOUR


async def test_list_busy_filters_out_other_managers(respx_mock: Any) -> None:
    adapter = make_talantix_adapter()
    mock_graphql(
        respx_mock,
        items=[meeting(meeting_id=78, start=NOW + HOUR, end=NOW + 2 * HOUR, manager_id=5)],
    )

    busy = await adapter.list_busy(manager_ids=["999"], start=NOW, end=NOW + timedelta(days=1))

    assert busy == []


async def test_find_free_slots_never_overlaps_busy_time(respx_mock: Any) -> None:
    """Главная гарантия: предложенный слот не пересекается с занятостью."""
    adapter = make_talantix_adapter()
    busy_start = NOW  # 10:00 по Москве
    busy_end = NOW + 4 * HOUR  # занимаем всё утро
    mock_graphql(
        respx_mock,
        items=[meeting(meeting_id=79, start=busy_start, end=busy_end)],
    )

    slots = await adapter.find_free_slots(
        manager_ids=["1"],
        start=NOW,
        end=NOW + timedelta(days=1),
        duration_minutes=60,
        limit=3,
    )

    assert slots, "после занятого интервала обязаны найтись свободные слоты"
    for slot in slots:
        assert not slot.overlaps(busy_start, busy_end), f"слот {slot.start} попал на занятое время"
        # Буфер после встречи: слот не должен начинаться сразу по её окончании.
        assert slot.start >= busy_end + 10 * MINUTE
        assert slot.duration == timedelta(minutes=60)


async def test_find_free_slots_does_not_offer_past_time(respx_mock: Any) -> None:
    adapter = make_talantix_adapter(min_lead_minutes=30)
    mock_graphql(respx_mock, items=[])

    slots = await adapter.find_free_slots(
        manager_ids=["1"],
        start=NOW,
        end=NOW + timedelta(days=5),
        duration_minutes=60,
        limit=3,
    )

    assert slots
    deadline = NOW + 30 * MINUTE
    for slot in slots:
        assert slot.start >= deadline, f"слот {slot.start} предлагается слишком рано"


async def test_find_free_slots_respects_limit(respx_mock: Any) -> None:
    adapter = make_talantix_adapter()
    mock_graphql(respx_mock, items=[])

    slots = await adapter.find_free_slots(
        manager_ids=["1"],
        start=NOW,
        end=NOW + timedelta(days=7),
        duration_minutes=60,
        limit=2,
    )

    assert len(slots) == 2


async def test_window_longer_than_platform_limit_is_rejected(respx_mock: Any) -> None:
    """Talantix отклоняет окно шире 93 дней: не доводим дело до его ошибки."""
    route = mock_graphql(respx_mock, items=[])
    adapter = make_talantix_adapter()

    with pytest.raises(ConfigError, match="93"):
        await adapter.list_busy(manager_ids=["1"], start=NOW, end=NOW + timedelta(days=100))

    assert not route.called, "заведомо неверный запрос не должен уходить в сеть"


async def test_inverted_window_is_rejected(respx_mock: Any) -> None:
    mock_graphql(respx_mock, items=[])
    adapter = make_talantix_adapter()

    with pytest.raises(ConfigError):
        await adapter.list_busy(manager_ids=["1"], start=NOW, end=NOW - HOUR)


async def test_platform_error_is_retryable(respx_mock: Any) -> None:
    """Внутренняя ошибка Talantix — временная: её можно повторить."""
    respx_mock.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "Internal server error", "extensions": {"errorType": "INTERNAL"}}]},
        )
    )
    adapter = make_talantix_adapter()

    with pytest.raises(ChannelUnavailable):
        await adapter.list_busy(manager_ids=["1"], start=NOW, end=NOW + timedelta(days=1))


async def test_query_defect_is_not_retryable(respx_mock: Any) -> None:
    """Наш сломанный запрос повторять бессмысленно — это ConfigError."""
    respx_mock.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={"errors": [{"message": "Query complexity exceeded the limit"}]},
        )
    )
    adapter = make_talantix_adapter()

    with pytest.raises(ConfigError, match="сложности"):
        await adapter.list_busy(manager_ids=["1"], start=NOW, end=NOW + timedelta(days=1))


async def test_create_meeting_sends_idempotency_marker(respx_mock: Any) -> None:
    """Маркер идемпотентности в теле запроса — основа верификации после таймаута."""
    route = respx_mock.post(GRAPHQL_URL).mock(
        return_value=httpx.Response(
            200,
            json={
                "data": {
                    "createCalendarMeeting": {
                        "__typename": "CalendarMeetingItem",
                        **meeting(
                            meeting_id=555,
                            start=NOW + 26 * HOUR,
                            end=NOW + 27 * HOUR,
                        ),
                    }
                }
            },
        )
    )
    adapter = make_talantix_adapter()

    created = await adapter.create_meeting(
        title="Собеседование: Python-разработчик",
        start=NOW + 26 * HOUR,
        end=NOW + 27 * HOUR,
        timezone="Europe/Moscow",
        manager_ids=["1"],
        candidate_email="candidate@example.com",
        description="Знакомство",
        idempotency_key="req-123",
    )

    assert created is not None
    # Адаптер сначала ищет уже созданную встречу по маркеру (защита от повтора
    # после таймаута) и только потом создаёт. Поэтому смотрим не первый вызов,
    # а именно мутацию создания.
    bodies = [call.request.content.decode("utf-8") for call in route.calls]
    mutations = [body for body in bodies if "createCalendarMeeting" in body]
    assert mutations, "мутация создания встречи не отправлялась"
    assert any("req-123" in body for body in mutations), (
        "маркер идемпотентности обязан попасть в тело запроса создания"
    )
