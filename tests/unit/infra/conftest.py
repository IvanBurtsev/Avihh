"""Общие фикстуры unit-тестов инфраструктуры.

Все тесты этой папки работают без сети, Redis и Docker: Redis-зависимые
сценарии проверяются на фейковом клиенте, а время подменяется ``FakeClock``.
Это принципиально: тест, которому нужен живой Redis, в CI либо флакует, либо
выключается — и тогда инфраструктура остаётся непроверенной.

Фабрика настроек — фикстура, а не импортируемый хелпер: тестовые каталоги не
являются пакетами (``__init__.py`` в них нет), и относительный импорт из
соседнего файла сделал бы тесты зависимыми от способа импорта pytest.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest

from hirebot.config import Settings
from hirebot.infra.clock import FakeClock
from hirebot.infra.security.crypto import FieldCipher


def _build_settings(**overrides: Any) -> Settings:
    """Настройки для тестов: без .env, без Redis, с валидным ключом ПДн."""
    base: dict[str, Any] = {
        "env": "test",
        "security": {"pii_key": FieldCipher.generate_key()},
        "redis": {"dsn": "redis://127.0.0.1:6379/0", "required": False},
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


@pytest.fixture
def fake_clock() -> FakeClock:
    """Виртуальное время: тесты не спят по-настоящему."""
    return FakeClock()


@pytest.fixture
def settings() -> Settings:
    return _build_settings()


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    """Фабрика настроек с переопределением отдельных секций."""
    return _build_settings


@pytest.fixture
def cipher(settings: Settings) -> FieldCipher:
    return FieldCipher.from_settings(settings)
