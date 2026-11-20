"""Фикстуры тестов устойчивости.

Все фикстуры — на fake-реализациях (``hirebot.testing.fakes``) и на боевом
``HttpxClient`` с перехваченным через ``respx`` транспортом: ни сети, ни БД,
ни ключей. Время виртуальное (``FIXED_NOW``), поэтому сценарии с таймаутами,
backoff и верификацией брони воспроизводимы до секунды.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from typing import Any

import pytest
from resilience_support import make_container, make_http_client

from hirebot.container import Container
from hirebot.infra.http import HttpxClient
from hirebot.testing.fakes import (
    FakeCalendarAdapter,
    FakeChannelAdapter,
    InMemoryJobQueue,
    fake_calendar,
    fake_channel,
    fake_queue,
)


@pytest.fixture
def container() -> Container:
    """Пустой fake-контейнер: время зафиксировано, данные готовит сам тест."""
    return make_container()


@pytest.fixture
def channel(container: Container) -> FakeChannelAdapter:
    """Канал площадки (fake): отклики, чаты, отправленные сообщения."""
    return fake_channel(container)


@pytest.fixture
def calendar(container: Container) -> FakeCalendarAdapter:
    """Календарь (fake): задаваемая занятость и созданные встречи."""
    return fake_calendar(container)


@pytest.fixture
def queue(container: Container) -> InMemoryJobQueue:
    """Очередь задач (fake): видно отложенные повторы outbox."""
    return fake_queue(container)


@pytest.fixture
async def http_client_factory() -> AsyncIterator[Callable[..., HttpxClient]]:
    """Фабрика боевых HTTP-клиентов с виртуальными часами.

    Клиенты создаются внутри теста (с нужным ``max_attempts``) и закрываются
    здесь же: ``httpx.AsyncClient`` нельзя оставлять открытым.
    """
    clients: list[HttpxClient] = []

    def factory(**options: Any) -> HttpxClient:
        client = make_http_client(**options)
        clients.append(client)
        return client

    yield factory
    for client in clients:
        await client.aclose()
