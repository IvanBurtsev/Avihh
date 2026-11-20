"""Тестовые двойники портов для тестов адаптеров.

Имя модуля намеренно уникально (``adapter_fakes``, а не ``support``): при
``importmode=prepend`` без ``__init__.py`` модули с одинаковым именем в разных
тестовых каталогах конфликтуют в ``sys.modules``.

* ``HttpxHttpClient`` — ``HttpClientPort`` поверх настоящего ``httpx``: ``respx``
  перехватывает запросы на том же уровне, на котором их делает боевой код;
* ``ExplodingHttpClient`` — транспорт, который падает при любой попытке запроса:
  так доказывается, что выключенный адаптер не выходит в сеть;
* ``MemoryCache`` и ``FakeClock`` — детерминированные подмены портов.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
from pydantic import SecretStr

from hirebot.adapters.calendar.talantix.adapter import TalantixCalendarAdapter
from hirebot.adapters.channels.hh.adapter import HhChannelAdapter
from hirebot.config import HHSettings, LlmSettings, TalantixSettings
from hirebot.ports.infra import ClockPort, HttpResponse, LockPort

__all__ = [
    "ExplodingHttpClient",
    "FakeClock",
    "HttpxHttpClient",
    "MemoryCache",
    "hh_settings",
    "llm_settings",
    "make_hh_adapter",
    "make_talantix_adapter",
    "talantix_settings",
]

#: Фиксированное «сейчас» тестов: 20 ноября 2026, 07:00 UTC = 10:00 в Москве.
NOW = datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


class HttpxHttpClient:
    """``HttpClientPort`` поверх ``httpx`` (для перехвата через ``respx``)."""

    def __init__(self) -> None:
        self._client = httpx.AsyncClient(timeout=5.0)

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        form: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = True,
        cache_ttl_s: float = 0.0,
        rate_limit_key: str | None = None,
        rate_limit_rps: float | None = None,
        bucket: str = "default",
    ) -> HttpResponse:
        response = await self._client.request(
            method,
            url,
            params=params,
            json=json_body,
            data=form,
            headers=dict(headers or {}),
        )
        body: Any = None
        if response.content:
            try:
                body = response.json()
            except ValueError:
                body = None
        return HttpResponse(
            status_code=response.status_code,
            body=body,
            text=response.text,
            headers=dict(response.headers),
            url=str(response.url),
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class ExplodingHttpClient:
    """Транспорт-«мина»: любой запрос — ошибка теста."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    async def request(self, method: str, url: str, **_kwargs: Any) -> HttpResponse:
        self.calls.append(f"{method} {url}")
        msg = f"Выключенный адаптер не должен делать запросы, но вызвал {method} {url}"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        return None


class FakeClock:
    """Детерминированные часы со сдвигом времени вручную."""

    def __init__(self, now: datetime | None = None) -> None:
        self._now = now or NOW

    def now(self) -> datetime:
        return self._now

    async def sleep(self, seconds: float) -> None:
        self._now += timedelta(seconds=seconds)

    def advance(self, **kwargs: float) -> None:
        self._now += timedelta(**kwargs)


class MemoryCache:
    """In-process ``CachePort`` с TTL (без Redis)."""

    def __init__(self, *, clock: ClockPort | None = None) -> None:
        self._clock = clock
        self.raw: dict[str, bytes] = {}
        self.json: dict[str, Any] = {}
        self.expires: dict[str, datetime] = {}
        self.reads: list[str] = []

    def _now(self) -> datetime:
        return self._clock.now() if self._clock is not None else datetime.now(tz=UTC)

    def _alive(self, key: str) -> bool:
        expires = self.expires.get(key)
        return expires is None or expires > self._now()

    async def get(self, key: str) -> bytes | None:
        self.reads.append(key)
        return self.raw.get(key) if self._alive(key) else None

    async def set(self, key: str, value: bytes, *, ttl_s: float | None = None) -> None:
        self.raw[key] = value
        self._touch(key, ttl_s)

    async def delete(self, key: str) -> None:
        self.raw.pop(key, None)
        self.json.pop(key, None)
        self.expires.pop(key, None)

    async def get_json(self, key: str) -> Any | None:
        self.reads.append(key)
        if key not in self.json or not self._alive(key):
            return None
        return self.json[key]

    async def set_json(self, key: str, value: Any, *, ttl_s: float | None = None) -> None:
        self.json[key] = value
        self._touch(key, ttl_s)

    async def incr(self, key: str, *, ttl_s: float | None = None) -> int:
        current = int(self.json.get(key) or 0) + 1
        await self.set_json(key, current, ttl_s=ttl_s)
        return current

    def _touch(self, key: str, ttl_s: float | None) -> None:
        if ttl_s is None:
            self.expires.pop(key, None)
        else:
            self.expires[key] = self._now() + timedelta(seconds=ttl_s)


def hh_settings(**overrides: Any) -> HHSettings:
    """Настройки hh.ru для тестов (токен задан, срок жизни неизвестен)."""
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://api.hh.ru",
        "client_id": "test-client",
        "client_secret": SecretStr("test-secret"),
        "access_token": SecretStr("test-access-token"),
        "refresh_token": None,
        "user_agent": "hirebot-test/0.1 (qa@example.com)",
    }
    base.update(overrides)
    return HHSettings(**base)


def talantix_settings(**overrides: Any) -> TalantixSettings:
    """Настройки Talantix для тестов (зона по умолчанию — Москва)."""
    base: dict[str, Any] = {
        "enabled": True,
        "base_url": "https://api.talantix.ru",
        "access_token": SecretStr("talantix-token"),
        "refresh_token": None,
        "default_timezone": "Europe/Moscow",
        "max_window_days": 90,
    }
    base.update(overrides)
    return TalantixSettings(**base)


def llm_settings(**overrides: Any) -> LlmSettings:
    """Настройки LLM для тестов: OpenAI-совместимый провайдер, ПДн не отправляем."""
    base: dict[str, Any] = {
        "provider": "openai_compat",
        "base_url": "https://llm.example.com/v1",
        "api_key": SecretStr("llm-key"),
        "model": "test-model",
        "temperature": 0.2,
        "max_tokens": 512,
        "cache_ttl_s": 3600,
        "send_pii": False,
    }
    base.update(overrides)
    return LlmSettings(**base)


def make_hh_adapter(
    *,
    settings: HHSettings | None = None,
    cache: MemoryCache | None = None,
    clock: FakeClock | None = None,
    locks: LockPort | None = None,
    http: Any | None = None,
) -> HhChannelAdapter:
    """Собрать адаптер hh.ru на тестовых зависимостях."""
    return HhChannelAdapter(
        settings or hh_settings(),
        http=http or HttpxHttpClient(),
        clock=clock or FakeClock(),
        cache=cache if cache is not None else MemoryCache(),
        locks=locks,
    )


def make_talantix_adapter(
    *,
    settings: TalantixSettings | None = None,
    cache: MemoryCache | None = None,
    clock: FakeClock | None = None,
    locks: LockPort | None = None,
    http: Any | None = None,
    **kwargs: Any,
) -> TalantixCalendarAdapter:
    """Собрать адаптер Talantix на тестовых зависимостях."""
    return TalantixCalendarAdapter(
        settings or talantix_settings(),
        http=http or HttpxHttpClient(),
        clock=clock or FakeClock(),
        cache=cache if cache is not None else MemoryCache(),
        locks=locks,
        **kwargs,
    )
