"""Инциденты транспорта: 429 с ``Retry-After`` и сетевой таймаут.

Проверяется слой, который принимает решения о повторах, — боевой
``HttpxClient`` (``src/hirebot/infra/http.py``) вместе с адаптером площадки.
Транспорт перехвачен ``respx``, часы виртуальные (``FakeClock``), поэтому
паузы между повторами видны в ``clock.slept`` и не замедляют тест.

Гарантии и их источник (ARCHITECTURE.md, п. 2.7 и 6.1):

* 429 нельзя повторять «в лоб»: площадка читает это как нарушение лимита;
  длинный ``Retry-After`` возвращается наверх как ``RateLimited`` с паузой;
* повторяются только идемпотентные запросы; создание встречи и отправка
  сообщения — однократные операции, слепой повтор создаёт дубль;
* таймаут идемпотентного чтения ретраится и заканчивается
  ``ChannelUnavailable``, а не «зависанием» воркера.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any

import httpx
import pytest
from resilience_support import (
    FIXED_NOW,
    http_url,
    make_hh_adapter,
    make_talantix_adapter,
    negotiation_payload,
)

from hirebot.domain.errors import ChannelUnavailable, RateLimited
from hirebot.infra.clock import FakeClock

HH_BASE = "https://api.hh.ru"
TALANTIX_BASE = "https://api.talantix.ru"

CONVERSATION_ID = "neg-1"

MAX_ATTEMPTS = 3


def long_retry_after_response() -> httpx.Response:
    """429 с длинной паузой: пережидать её внутри запроса нельзя."""
    return httpx.Response(
        429,
        json={"type": "resumes", "value": "view_limit_exceeded"},
        headers={"Retry-After": "60"},
    )


def short_retry_after_response() -> httpx.Response:
    """429 с короткой паузой — случай, который транспорт вправе переждать."""
    return httpx.Response(
        429,
        json={"value": "in_a_row_limit"},
        headers={"Retry-After": "1"},
    )


async def test_rate_limited_read_reports_retry_after_without_blind_retries(
    respx_mock: Any,
    http_client_factory: Any,
) -> None:
    """429 на идемпотентном чтении: ``RateLimited`` с паузой и ровно один запрос."""
    clock = FakeClock(FIXED_NOW)
    client = http_client_factory(clock=clock, max_attempts=MAX_ATTEMPTS, max_retry_after_s=2.0)
    hh = make_hh_adapter(client, clock=clock)
    route = respx_mock.get(http_url(HH_BASE, "/resumes/resume-1")).mock(
        return_value=long_retry_after_response()
    )

    with pytest.raises(RateLimited) as excinfo:
        await hh.fetch_resume("resume-1")

    assert excinfo.value.retry_after_s == 60.0
    assert route.call_count == 1, "429 с длинной паузой повторён «в лоб»"
    assert clock.slept == 0.0


async def test_rate_limited_action_is_not_retried(
    respx_mock: Any,
    http_client_factory: Any,
) -> None:
    """429 на неидемпотентном действии: одна попытка и пауза, без слепых повторов."""
    clock = FakeClock(FIXED_NOW)
    client = http_client_factory(clock=clock, max_attempts=MAX_ATTEMPTS, max_retry_after_s=2.0)
    hh = make_hh_adapter(client, clock=clock)
    respx_mock.get(http_url(HH_BASE, f"/negotiations/{CONVERSATION_ID}")).mock(
        return_value=httpx.Response(200, json=negotiation_payload(conversation_id=CONVERSATION_ID))
    )
    route = respx_mock.post(http_url(HH_BASE, f"/negotiations/{CONVERSATION_ID}/messages")).mock(
        return_value=short_retry_after_response()
    )

    with pytest.raises(ChannelUnavailable) as excinfo:
        await hh.send_message(CONVERSATION_ID, "текст", idempotency_key="msg-1")

    assert route.call_count == 1, "отправка сообщения повторена вслепую"
    assert excinfo.value.retry_after_s == 1.0


async def test_rate_limited_non_idempotent_action_is_reported_as_rate_limited(
    respx_mock: Any,
    http_client_factory: Any,
) -> None:
    """429 приходит вызывающей стороне как ``RateLimited`` независимо от вида запроса."""
    clock = FakeClock(FIXED_NOW)
    client = http_client_factory(clock=clock, max_attempts=MAX_ATTEMPTS, max_retry_after_s=2.0)
    hh = make_hh_adapter(client, clock=clock)
    respx_mock.get(http_url(HH_BASE, f"/negotiations/{CONVERSATION_ID}")).mock(
        return_value=httpx.Response(200, json=negotiation_payload(conversation_id=CONVERSATION_ID))
    )
    respx_mock.post(http_url(HH_BASE, f"/negotiations/{CONVERSATION_ID}/messages")).mock(
        return_value=short_retry_after_response()
    )

    with pytest.raises(RateLimited):
        await hh.send_message(CONVERSATION_ID, "текст", idempotency_key="msg-2")


async def test_timeout_of_idempotent_read_is_retried_then_reported(
    respx_mock: Any,
    http_client_factory: Any,
) -> None:
    """Таймаут идемпотентного чтения: повторы по числу попыток и ``ChannelUnavailable``."""
    clock = FakeClock(FIXED_NOW)
    client = http_client_factory(clock=clock, max_attempts=MAX_ATTEMPTS)
    hh = make_hh_adapter(client, clock=clock)
    route = respx_mock.get(http_url(HH_BASE, "/resumes/resume-2")).mock(
        side_effect=httpx.ConnectTimeout("таймаут соединения")
    )

    with pytest.raises(ChannelUnavailable) as excinfo:
        await hh.fetch_resume("resume-2")

    assert not isinstance(excinfo.value, RateLimited)
    assert route.call_count == MAX_ATTEMPTS, "идемпотентное чтение не повторили"
    assert clock.slept > 0.0, "повторы шли без паузы"


async def test_timeout_of_meeting_creation_is_not_retried(
    respx_mock: Any,
    http_client_factory: Any,
) -> None:
    """Таймаут создания встречи: попытка ровно одна, повтор создал бы дубль."""
    clock = FakeClock(FIXED_NOW)
    client = http_client_factory(clock=clock, max_attempts=MAX_ATTEMPTS)
    calendar = make_talantix_adapter(client, clock=clock)
    route = respx_mock.post(http_url(TALANTIX_BASE, "/graphql")).mock(
        side_effect=httpx.ReadTimeout("таймаут чтения")
    )
    start = FIXED_NOW + timedelta(hours=1)

    with pytest.raises(ChannelUnavailable):
        await calendar.create_meeting(
            title="Собеседование",
            start=start,
            end=start + timedelta(hours=1),
            timezone="Europe/Moscow",
            manager_ids=["1"],
        )

    assert route.call_count == 1, "создание встречи повторено вслепую"
    assert clock.slept == 0.0
