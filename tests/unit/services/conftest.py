"""Фикстуры тестов сервисного слоя.

Все фикстуры — на fake-реализациях (``hirebot.testing.fakes``). Контейнер
собирается пустым: тесты сами решают, какие данные им нужны, чтобы проверки
не зависели от демонстрационного набора.
"""

from __future__ import annotations

import pytest
from services_support import make_settings

from hirebot.container import Container
from hirebot.testing.fakes import (
    FakeCalendarAdapter,
    FakeChannelAdapter,
    FakeLlmAdapter,
    InMemoryJobQueue,
    build_fake_container,
    fake_calendar,
    fake_channel,
    fake_llm,
    fake_queue,
)


@pytest.fixture
def container() -> Container:
    """Пустой fake-контейнер (без демонстрационных данных)."""
    return build_fake_container(make_settings(), seed_demo=False)


@pytest.fixture
def demo_container() -> Container:
    """Fake-контейнер с демонстрационными данными."""
    return build_fake_container(make_settings())


@pytest.fixture
def channel(container: Container) -> FakeChannelAdapter:
    return fake_channel(container)


@pytest.fixture
def calendar(container: Container) -> FakeCalendarAdapter:
    return fake_calendar(container)


@pytest.fixture
def llm(container: Container) -> FakeLlmAdapter:
    return fake_llm(container)


@pytest.fixture
def queue(container: Container) -> InMemoryJobQueue:
    return fake_queue(container)
