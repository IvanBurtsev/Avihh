"""Ленивое подключение сервисов внутри воркера.

``hirebot.services.*`` пишется параллельно со слоем доставки, поэтому импорт
делается в момент вызова, а не на уровне модуля: воркер (и его тесты) должны
собираться и запускаться независимо от готовности сервисного слоя.

Функции модуля — единственная точка, через которую задачи воркера получают
сервисы. Тесты подменяют их (``monkeypatch.setattr(services, "ingest_service", ...)``)
и проверяют, что задача вызвала сервис с правильными аргументами.
"""

from __future__ import annotations

from importlib import import_module
from typing import Any

from hirebot.container import Container


class ServiceUnavailableError(RuntimeError):
    """Сервис не найден в сборке (модуль ещё не реализован или переименован)."""


def _resolve(module: str, attribute: str, container: Container) -> Any:
    """Создать сервис ``module.attribute(container)``.

    Возвращается ``Any``: контракт очереди не должен зависеть от типов сервисов.
    """
    try:
        factory = getattr(import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        msg = f"Сервис {module}.{attribute} недоступен: {exc}"
        raise ServiceUnavailableError(msg) from exc
    service: Any = factory(container)
    return service


def ingest_service(container: Container) -> Any:
    """``IngestService``: поллинг площадок и приём откликов."""
    return _resolve("hirebot.services.ingest", "IngestService", container)


def conversation_service(container: Container) -> Any:
    """``ConversationService``: шаг воронки по диалогу."""
    return _resolve("hirebot.services.conversation", "ConversationService", container)


def screening_service(container: Container) -> Any:
    """``ScreeningService``: скоринг резюме под вакансию."""
    return _resolve("hirebot.services.screening", "ScreeningService", container)


def scheduling_service(container: Container) -> Any:
    """``SchedulingService``: подбор слотов и бронирование."""
    return _resolve("hirebot.services.scheduling", "SchedulingService", container)


def outbox_dispatcher(container: Container) -> Any:
    """``OutboxDispatcher``: доставка накопленных исходящих действий."""
    return _resolve("hirebot.services.outbox", "OutboxDispatcher", container)


__all__ = [
    "ServiceUnavailableError",
    "conversation_service",
    "ingest_service",
    "outbox_dispatcher",
    "scheduling_service",
    "screening_service",
]
