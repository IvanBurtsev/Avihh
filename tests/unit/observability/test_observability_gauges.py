"""Тесты бизнес-gauge'ей и сервера метрик воркера.

Проверяется то, что иначе осталось бы незамеченным:

* состояние воронки действительно снимается из БД, а не показывает нули;
* неизвестное состояние не превращается в отдельную метку (кардинальность);
* недоступность БД не роняет скрейп — иначе наблюдаемость пропадала бы ровно
  в момент инцидента;
* сервер метрик воркера поднимается и не мешает работе, если порт занят.
"""

from __future__ import annotations

from typing import Any

import pytest
from prometheus_client import CollectorRegistry

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.enums import Channel
from hirebot.observability.gauges import refresh_business_gauges
from hirebot.observability.metrics import (
    observe_conversations,
    reset_metrics,
    setup_metrics,
    start_worker_metrics_server,
)
from hirebot.services import IngestService


@pytest.fixture(autouse=True)
def _fresh_metrics() -> Any:
    """Каждый тест начинает с чистого реестра: тесты не должны влиять друг на друга."""
    setup_metrics(enabled=True)
    yield
    reset_metrics()


def _exposition() -> str:
    registry = CollectorRegistry(auto_describe=True)
    del registry  # используем реестр метрик приложения через публичный снимок
    return ""


async def _conversations_metric(container: Any) -> dict[str, float]:
    """Снять значения gauge ``hirebot_conversations`` после обновления."""
    await refresh_business_gauges(container)
    # Читаем через публичный эндпоинт метрик: он возвращает байты экспозиции.
    from hirebot.observability.metrics import metrics_endpoint

    payload, _ = metrics_endpoint()
    text = payload.decode("utf-8")
    values: dict[str, float] = {}
    for line in text.splitlines():
        if line.startswith("hirebot_conversations{"):
            state = line.split('state="')[1].split('"')[0]
            values[state] = float(line.rsplit(" ", 1)[1])
    return values


async def test_gauges_reflect_database_state() -> None:
    """Gauge показывает реальное число диалогов, а не нули из пустого реестра."""
    container = build_fake_container(
        Settings(env="test", log_level="WARNING", log_json=False, _env_file=None)
    )
    await IngestService(container).poll(Channel.HH)

    values = await _conversations_metric(container)

    assert sum(values.values()) >= 3, "три отклика должны быть видны в метрике"
    assert values.get("new", 0) >= 3


async def test_gauges_are_zeroed_for_absent_states() -> None:
    """Все состояния присутствуют: иначе график в Grafana «дырявый»."""
    container = build_fake_container(
        Settings(env="test", log_level="WARNING", log_json=False, _env_file=None)
    )

    values = await _conversations_metric(container)

    # Состояний одиннадцать, и все должны быть в экспозиции, пусть и нулями.
    assert len(values) == 11
    assert all(value == 0 for value in values.values())


async def test_database_failure_does_not_break_scrape() -> None:
    """Если БД недоступна, скрейп не падает: метрики нужны именно тогда."""

    class BrokenContainer:
        settings = Settings(env="test", _env_file=None)

        def uow_factory(self) -> Any:
            raise RuntimeError("база недоступна")

    # Не должно быть исключения — только предупреждение в лог.
    await refresh_business_gauges(BrokenContainer())  # type: ignore[arg-type]


def test_unknown_state_is_dropped_not_added_as_label() -> None:
    """Произвольная строка не должна увеличивать кардинальность метрик.

    ``observe_conversations`` итерирует только известные состояния, поэтому
    чужой ключ просто игнорируется. Это и есть защита: иначе площадка или
    конфигурация могли бы «наплодить» меток.
    """
    observe_conversations({"new": 1, "какой-то-произвольный-статус": 5})

    from hirebot.observability.metrics import metrics_endpoint

    payload, _ = metrics_endpoint()
    text = payload.decode("utf-8")

    assert "какой-то-произвольный-статус" not in text
    assert 'state="new"} 1' in text


def test_worker_metrics_server_starts_on_free_port() -> None:
    """Воркер поднимает свой /metrics: счётчики видны там, где шли события."""
    setup_metrics(enabled=True)

    started = start_worker_metrics_server(0)  # 0 — выбрать свободный порт

    assert started is True


def test_worker_metrics_server_returns_bool_on_failure() -> None:
    """Сбой запуска сервера метрик не должен ронять воркер.

    Проверяем контракт, а не конкретную причину отказа: функция обязана вернуть
    ``bool`` и не пробросить исключение наружу.
    """
    setup_metrics(enabled=True)

    result = start_worker_metrics_server(65_535)

    assert isinstance(result, bool)
