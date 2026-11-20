"""Фикстуры тестов адаптеров.

Тесты не ходят в сеть: HTTP-трафик перехватывается ``respx`` на уровне ``httpx``
(``HttpxHttpClient``), кэш и часы подменены детерминированными реализациями.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from adapter_fakes import FakeClock, HttpxHttpClient, MemoryCache, make_hh_adapter, make_talantix_adapter


@pytest.fixture
async def http() -> AsyncIterator[HttpxHttpClient]:
    """Транспорт поверх httpx: запросы перехватывает respx."""
    client = HttpxHttpClient()
    yield client
    await client.aclose()


@pytest.fixture
def cache() -> MemoryCache:
    return MemoryCache()


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def hh(http, cache, clock):
    """Фабрика адаптера hh.ru: любую зависимость можно подменить в одном тесте."""

    def _factory(settings=None, locks=None, http_client=None, cache_store=None, clock_source=None):
        return make_hh_adapter(
            settings=settings,
            locks=locks,
            http=http_client if http_client is not None else http,
            cache=cache_store if cache_store is not None else cache,
            clock=clock_source if clock_source is not None else clock,
        )

    return _factory


@pytest.fixture
def talantix(http, cache, clock):
    """Фабрика адаптера Talantix: любую зависимость можно подменить в одном тесте."""

    def _factory(
        settings=None,
        locks=None,
        http_client=None,
        cache_store=None,
        clock_source=None,
        **calendar_options,
    ):
        return make_talantix_adapter(
            settings=settings,
            locks=locks,
            http=http_client if http_client is not None else http,
            cache=cache_store if cache_store is not None else cache,
            clock=clock_source if clock_source is not None else clock,
            **calendar_options,
        )

    return _factory
