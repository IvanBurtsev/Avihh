"""Зависимости FastAPI: контейнер, настройки, аутентификация и сервисы.

Сервисы подключаются **через зависимости**, а не импортируются в роутерах. Это
даёт две вещи:

* тесты подменяют сервис одним ``app.dependency_overrides[get_ingest_service]``,
  без сети, БД и Redis;
* ``hirebot.services.*`` грузится лениво (``importlib``), поэтому API собирается и
  импортируется даже в тот момент, когда сервисный слой ещё не написан, —
  в этом случае эндпоинт честно отвечает ``503``.
"""

from __future__ import annotations

import secrets as secrets_module
from importlib import import_module
from typing import Annotated, Any

from fastapi import Depends, HTTPException, Request, status

from hirebot.config import Settings
from hirebot.config import get_settings as get_cached_settings
from hirebot.container import Container
from hirebot.logging_setup import get_logger

logger = get_logger(__name__)

#: Заголовок с ключом административного доступа.
API_KEY_HEADER = "X-API-Key"

#: Предупреждение об открытом доступе печатаем один раз за процесс.
_warned_open_access = False


def get_container(request: Request) -> Container:
    """Достать контейнер приложения.

    Контейнер собирается в ``lifespan`` (или подкладывается тестом) и живёт в
    ``app.state``. Если его нет — приложение поднято неправильно, и это ``503``,
    а не ``500``: клиенту имеет смысл повторить запрос.
    """
    container = getattr(request.app.state, "container", None)
    if not isinstance(container, Container):
        msg = "DI-контейнер не собран: приложение запущено без startup-фазы"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)
    return container


def get_settings(request: Request) -> Settings:
    """Настройки приложения (из контейнера, иначе из кэша процесса)."""
    container = getattr(request.app.state, "container", None)
    if isinstance(container, Container):
        return container.settings
    return get_cached_settings()


ContainerDep = Annotated[Container, Depends(get_container)]
SettingsDep = Annotated[Settings, Depends(get_settings)]


def _configured_secret(settings: Settings) -> str | None:
    """Секрет вебхука в виде строки (``SecretStr`` не должен утекать в логи)."""
    secret = settings.security.webhook_secret
    if secret is None:
        return None
    value = secret.get_secret_value().strip()
    return value or None


def require_api_key(
    request: Request,
    settings: SettingsDep,
) -> None:
    """Проверить заголовок ``X-API-Key``.

    Ключ — это ``settings.security.webhook_secret`` (отдельного поля в конфиге
    нет). Если секрет не задан:

    * окружение ``local``/``test`` — доступ открыт, но в лог пишется
      предупреждение (демо и тесты не должны требовать секретов);
    * любое другое окружение — ``503``: небезопасная конфигурация не должна
      приводить к молчаливо открытому API.
    """
    # Одно предупреждение на процесс: иначе лог зальёт каждый запрос.
    global _warned_open_access
    expected = _configured_secret(settings)
    if expected is None:
        if settings.is_local:
            if not _warned_open_access:
                _warned_open_access = True
                logger.warning(
                    "api_key_not_configured",
                    hint="HIREBOT_SECURITY__WEBHOOK_SECRET не задан: API открыт без ключа",
                    env=settings.env,
                )
            return
        msg = "API-ключ не сконфигурирован: задайте HIREBOT_SECURITY__WEBHOOK_SECRET"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)

    provided = request.headers.get(API_KEY_HEADER, "")
    if not provided or not secrets_module.compare_digest(provided.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=f"Неверный или отсутствующий заголовок {API_KEY_HEADER}",
        )


def webhook_secret_or_none(settings: Settings) -> str | None:
    """Секрет для проверки подписи вебхука.

    ``None`` означает «подпись не проверяется» — это допустимо только локально.
    В остальных окружениях отсутствие секрета приводит к ``503``.
    """
    expected = _configured_secret(settings)
    if expected is None and not settings.is_local:
        msg = "Приём вебхуков отключён: не задан HIREBOT_SECURITY__WEBHOOK_SECRET для нелокального окружения"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg)
    return expected


def resolve_service(module: str, attribute: str, container: Container) -> Any:
    """Лениво создать сервис из ``hirebot.services.*``.

    Возвращаем ``Any``: слой доставки не должен зависеть от конкретных типов
    сервисов (они живут в другом пакете и пишутся параллельно). Проверка ошибок
    конфигурации — здесь: отсутствующий модуль превращается в понятный ``503``.
    """
    try:
        factory = getattr(import_module(module), attribute)
    except (ImportError, AttributeError) as exc:
        logger.error("service_unavailable", service=f"{module}.{attribute}", error=str(exc))
        msg = f"Сервис {attribute} недоступен в этой сборке"
        raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=msg) from exc
    service: Any = factory(container)
    return service


def get_ingest_service(container: ContainerDep) -> Any:
    """``IngestService``: поллинг площадок и приём откликов."""
    return resolve_service("hirebot.services.ingest", "IngestService", container)


def get_conversation_service(container: ContainerDep) -> Any:
    """``ConversationService``: шаг воронки по диалогу."""
    return resolve_service("hirebot.services.conversation", "ConversationService", container)


def get_scheduling_service(container: ContainerDep) -> Any:
    """``SchedulingService``: слоты и бронирование собеседования."""
    return resolve_service("hirebot.services.scheduling", "SchedulingService", container)


IngestServiceDep = Annotated[Any, Depends(get_ingest_service)]
ConversationServiceDep = Annotated[Any, Depends(get_conversation_service)]
SchedulingServiceDep = Annotated[Any, Depends(get_scheduling_service)]

__all__ = [
    "API_KEY_HEADER",
    "ContainerDep",
    "ConversationServiceDep",
    "IngestServiceDep",
    "SchedulingServiceDep",
    "SettingsDep",
    "get_container",
    "get_conversation_service",
    "get_ingest_service",
    "get_scheduling_service",
    "get_settings",
    "require_api_key",
    "resolve_service",
    "webhook_secret_or_none",
]
