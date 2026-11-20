"""Фикстуры тестов HTTP API.

Приложение собирается на fake-контейнере (без сети, БД и Redis), сервисный слой
подменяется через ``app.dependency_overrides`` — так тесты не зависят от
готовности ``hirebot.services.*`` и одновременно проверяют, что роутеры вызывают
сервисы с правильными аргументами.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
from api_support import (
    FakeCache,
    FakeQueue,
    FakeServices,
    FakeStore,
    asgi_client,
    make_container,
    make_settings,
)
from fastapi import FastAPI
from httpx import AsyncClient

from hirebot.api.app import create_app
from hirebot.api.deps import get_conversation_service, get_ingest_service, get_scheduling_service
from hirebot.config import Settings
from hirebot.container import Container


@pytest.fixture
def settings() -> Settings:
    """Настройки локального окружения: доступ открыт, секретов нет."""
    return make_settings()


@pytest.fixture
def store() -> FakeStore:
    """In-memory хранилище данных теста."""
    return FakeStore()


@pytest.fixture
def cache() -> FakeCache:
    """Кэш: используется идемпотентностью вебхуков."""
    return FakeCache()


@pytest.fixture
def queue() -> FakeQueue:
    """Очередь задач: проверяем, что задачи действительно ставятся."""
    return FakeQueue()


@pytest.fixture
def container(settings: Settings, store: FakeStore, cache: FakeCache, queue: FakeQueue) -> Container:
    """Контейнер на заглушках портов."""
    return make_container(settings, store=store, cache=cache, queue=queue)


@pytest.fixture
def app(container: Container) -> FastAPI:
    """Приложение с подложенным контейнером (lifespan его не пересобирает)."""
    return create_app(container=container)


@pytest.fixture
def services(app: FastAPI) -> FakeServices:
    """Подменить сервисный слой fake-реализациями."""
    bundle = FakeServices()
    app.dependency_overrides[get_ingest_service] = lambda: bundle.ingest
    app.dependency_overrides[get_conversation_service] = lambda: bundle.conversation
    app.dependency_overrides[get_scheduling_service] = lambda: bundle.scheduling
    return bundle


@pytest.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент поверх ASGI-транспорта."""
    async with asgi_client(app) as http_client:
        yield http_client


@pytest.fixture
async def raw_client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    """Клиент, который получает ответ ``500`` вместо проброса исключения."""
    async with asgi_client(app, raise_app_exceptions=False) as http_client:
        yield http_client
