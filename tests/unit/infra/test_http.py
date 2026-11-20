"""Тесты HTTP-клиента через ``respx``: ретраи, ошибки, кэш, редакция секретов.

Проверяется именно поведение порта, а не httpx: адаптеры площадок полагаются на
то, что 503 будет повторён, 401 — нет, а токен не попадёт в лог.
"""

from __future__ import annotations

import inspect
from typing import Any

import httpx
import orjson
import pytest
import respx

from hirebot.config import Settings
from hirebot.domain.errors import AuthExpired, ChannelUnavailable, RateLimited
from hirebot.infra import http as http_module
from hirebot.infra.cache import MemoryCache
from hirebot.infra.clock import FakeClock
from hirebot.infra.http import (
    REDACTED,
    HttpxClient,
    create_http_client,
    redact_headers,
    redact_params,
    redact_url,
)
from hirebot.infra.locks import MemoryLock
from hirebot.infra.ratelimit import TokenBucketRateLimiter
from hirebot.ports.infra import CachePort, HttpClientPort

BASE_URL = "https://api.example.test"


class _LogRecorder:
    """Двойник structlog-логгера: собирает события для проверки редакции."""

    def __init__(self) -> None:
        self.events: list[tuple[str, dict[str, Any]]] = []

    def debug(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def info(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def warning(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def error(self, event: str, **kwargs: Any) -> None:
        self.events.append((event, kwargs))

    def dump(self) -> str:
        return orjson.dumps([{event: kwargs} for event, kwargs in self.events]).decode()


def _client(
    *,
    clock: FakeClock | None = None,
    cache: CachePort | None = None,
    max_attempts: int = 3,
    max_retry_after_s: float = 2.0,
) -> HttpxClient:
    shared_clock = clock if clock is not None else FakeClock()
    return HttpxClient(
        cache=cache if cache is not None else MemoryCache(clock=shared_clock),
        rate_limiter=TokenBucketRateLimiter(clock=shared_clock),
        locks=MemoryLock(),
        clock=shared_clock,
        default_headers={"User-Agent": "hirebot-tests/1.0"},
        default_rate_per_second=1000.0,
        default_burst=100,
        max_attempts=max_attempts,
        max_retry_after_s=max_retry_after_s,
    )


async def test_retries_idempotent_request_on_5xx() -> None:
    clock = FakeClock()
    client = _client(clock=clock)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/ping").mock(
            side_effect=[
                httpx.Response(503),
                httpx.Response(502),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        response = await client.request("GET", f"{BASE_URL}/ping")

    assert route.call_count == 3
    assert response.status_code == 200
    assert response.body == {"ok": True}
    assert response.from_cache is False
    # Паузы выдерживались, но виртуально.
    assert clock.slept > 0
    await client.aclose()


async def test_reraises_channel_unavailable_after_retries() -> None:
    client = _client(max_attempts=2)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/down").mock(return_value=httpx.Response(500))
        with pytest.raises(ChannelUnavailable, match="500"):
            await client.request("GET", f"{BASE_URL}/down")

    assert route.call_count == 2
    await client.aclose()


async def test_non_idempotent_request_is_not_retried() -> None:
    client = _client()
    with respx.mock(assert_all_called=False) as router:
        route = router.post(f"{BASE_URL}/meetings").mock(return_value=httpx.Response(503))
        with pytest.raises(ChannelUnavailable):
            await client.request("POST", f"{BASE_URL}/meetings", json_body={"slot": 1}, idempotent=False)

    assert route.call_count == 1
    await client.aclose()


async def test_429_with_large_retry_after_is_raised_immediately() -> None:
    client = _client()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/limit").mock(
            return_value=httpx.Response(429, headers={"Retry-After": "60"})
        )
        with pytest.raises(RateLimited) as excinfo:
            await client.request("GET", f"{BASE_URL}/limit")

    assert route.call_count == 1
    assert excinfo.value.retry_after_s == pytest.approx(60.0)
    await client.aclose()


async def test_429_with_short_retry_after_is_honored_and_retried() -> None:
    clock = FakeClock()
    client = _client(clock=clock)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/limit").mock(
            side_effect=[
                httpx.Response(429, headers={"Retry-After": "1"}),
                httpx.Response(200, json={"ok": True}),
            ]
        )
        response = await client.request("GET", f"{BASE_URL}/limit")

    assert route.call_count == 2
    assert response.status_code == 200
    assert clock.slept == pytest.approx(1.0)
    await client.aclose()


async def test_429_without_retry_after_is_terminal() -> None:
    client = _client()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/limit").mock(return_value=httpx.Response(429))
        with pytest.raises(RateLimited) as excinfo:
            await client.request("GET", f"{BASE_URL}/limit")

    assert route.call_count == 1
    assert excinfo.value.retry_after_s is None
    await client.aclose()


async def test_401_raises_auth_expired_without_retry() -> None:
    client = _client()
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/me").mock(return_value=httpx.Response(401))
        with pytest.raises(AuthExpired):
            await client.request("GET", f"{BASE_URL}/me")

    assert route.call_count == 1
    await client.aclose()


async def test_timeout_becomes_channel_unavailable() -> None:
    client = _client(max_attempts=2)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/slow").mock(side_effect=httpx.ConnectTimeout("boom"))
        with pytest.raises(ChannelUnavailable, match="таймаут"):
            await client.request("GET", f"{BASE_URL}/slow")

    assert route.call_count == 2
    await client.aclose()


async def test_4xx_response_is_returned_to_adapter() -> None:
    """403/404 — не ошибка транспорта: адаптер сам решает (квота, скрытое резюме)."""
    client = _client()
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/resume/1").mock(
            return_value=httpx.Response(403, json={"error": "quota_exceeded"})
        )
        response = await client.request("GET", f"{BASE_URL}/resume/1")

    assert response.status_code == 403
    assert response.ok is False
    assert response.body == {"error": "quota_exceeded"}
    await client.aclose()


async def test_get_response_is_cached(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)
    client = _client(clock=fake_clock, cache=cache)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/dictionary").mock(
            return_value=httpx.Response(200, json={"items": [1, 2, 3]})
        )
        first = await client.request("GET", f"{BASE_URL}/dictionary", cache_ttl_s=60)
        second = await client.request("GET", f"{BASE_URL}/dictionary", cache_ttl_s=60)

    assert route.call_count == 1
    assert first.from_cache is False
    assert second.from_cache is True
    assert second.body == {"items": [1, 2, 3]}
    await client.aclose()


async def test_cache_key_depends_on_params(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)
    client = _client(clock=fake_clock, cache=cache)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/areas").mock(return_value=httpx.Response(200, json={"ok": True}))
        await client.request("GET", f"{BASE_URL}/areas", params={"q": "msk"}, cache_ttl_s=60)
        await client.request("GET", f"{BASE_URL}/areas", params={"q": "spb"}, cache_ttl_s=60)

    assert route.call_count == 2
    await client.aclose()


async def test_error_responses_are_not_cached(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)
    client = _client(clock=fake_clock, cache=cache, max_attempts=1)
    with respx.mock(assert_all_called=False) as router:
        route = router.get(f"{BASE_URL}/flaky").mock(
            side_effect=[httpx.Response(500), httpx.Response(200, json={"ok": True})]
        )
        with pytest.raises(ChannelUnavailable):
            await client.request("GET", f"{BASE_URL}/flaky", cache_ttl_s=60)
        response = await client.request("GET", f"{BASE_URL}/flaky", cache_ttl_s=60)

    assert route.call_count == 2
    assert response.from_cache is False
    assert response.body == {"ok": True}
    await client.aclose()


async def test_secrets_are_redacted_in_logs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    recorder = _LogRecorder()
    monkeypatch.setattr(http_module, "logger", recorder)
    client = _client()
    secret = "SUPER-SECRET-TOKEN"
    api_key = "API-KEY-VALUE"
    password = "P@ssw0rd"
    with respx.mock(assert_all_called=False) as router:
        router.get(f"{BASE_URL}/secure").mock(return_value=httpx.Response(200, json={"ok": True}))
        await client.request(
            "GET",
            f"{BASE_URL}/secure",
            headers={"Authorization": f"Bearer {secret}", "X-Api-Key": api_key},
            params={"access_token": secret, "client_secret": password, "page": 1},
        )

    dumped = recorder.dump()
    assert secret not in dumped
    assert api_key not in dumped
    assert password not in dumped
    assert REDACTED in dumped
    request_events = [payload for event, payload in recorder.events if event == "http_request"]
    assert request_events, "событие http_request должно логироваться"
    logged = request_events[0]
    assert logged["headers"]["Authorization"] == REDACTED
    assert logged["headers"]["X-Api-Key"] == REDACTED
    assert logged["params"]["access_token"] == REDACTED
    assert logged["params"]["client_secret"] == REDACTED
    assert logged["params"]["page"] == 1
    assert secret not in str(logged["url"])
    await client.aclose()


async def test_bodies_are_not_logged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Тело резюме — ПДн: в лог попадает только размер, не содержимое."""
    recorder = _LogRecorder()
    monkeypatch.setattr(http_module, "logger", recorder)
    client = _client()
    resume_text = "Иван Петров, телефон +7 999 123-45-67"
    with respx.mock(assert_all_called=False) as router:
        router.post(f"{BASE_URL}/screen").mock(return_value=httpx.Response(200, json={"score": 80}))
        await client.request("POST", f"{BASE_URL}/screen", json_body={"text": resume_text})

    dumped = recorder.dump()
    assert "Петров" not in dumped
    assert "+7 999" not in dumped
    assert "json_bytes" in dumped
    await client.aclose()


async def test_aclose_closes_underlying_client() -> None:
    client = _client()

    await client.aclose()

    assert client.client.is_closed is True


def test_redact_headers_is_case_insensitive() -> None:
    redacted = redact_headers({"authorization": "a", "COOKIE": "b", "Accept": "json"})

    assert redacted == {"authorization": REDACTED, "COOKIE": REDACTED, "Accept": "json"}
    assert redact_headers(None) == {}


def test_redact_params_covers_known_secrets() -> None:
    redacted = redact_params({"access_token": "t", "client_secret": "s", "password": "p", "limit": 10})

    assert redacted == {
        "access_token": REDACTED,
        "client_secret": REDACTED,
        "password": REDACTED,
        "limit": 10,
    }
    assert redact_params(None) == {}


def test_redact_url_keeps_query_shape() -> None:
    url = "https://api.hh.ru/token?grant_type=refresh_token&refresh_token=SECRET&client_id=abc"

    redacted = redact_url(url)

    assert "SECRET" not in redacted
    assert "grant_type=refresh_token" in redacted
    assert "client_id=abc" in redacted
    assert redact_url("https://api.hh.ru/vacancies") == "https://api.hh.ru/vacancies"


def test_create_http_client_is_sync_factory(settings: Settings) -> None:
    assert inspect.iscoroutinefunction(create_http_client) is False

    client = create_http_client(
        settings,
        cache=MemoryCache(),
        rate_limiter=TokenBucketRateLimiter(),
        locks=MemoryLock(),
        clock=FakeClock(),
    )

    assert isinstance(client, HttpClientPort)
    assert isinstance(client, HttpxClient)
    assert client.client.headers["user-agent"] == settings.hh.user_agent


def test_default_rate_limit_uses_strictest_channel(settings: Settings) -> None:
    """Без явного rate_limit_rps берётся самый строгий лимит из настроек."""
    client = create_http_client(
        settings,
        cache=MemoryCache(),
        rate_limiter=TokenBucketRateLimiter(),
        locks=MemoryLock(),
        clock=FakeClock(),
    )
    assert isinstance(client, HttpxClient)
    expected = min(
        settings.hh.rate_limit.requests_per_second,
        settings.avito.rate_limit.requests_per_second,
        settings.talantix.rate_limit.requests_per_second,
    )
    assert client._default_rps == pytest.approx(expected)
