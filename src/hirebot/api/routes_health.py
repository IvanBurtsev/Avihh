"""Проверки живости и готовности сервиса.

* ``GET /health`` — liveness: процесс отвечает. Никаких внешних вызовов, иначе
  мониторинг начнёт перезапускать контейнер из-за недоступности площадки.
* ``GET /health/ready`` — readiness: доступна ли БД и живы ли включённые каналы.
* ``GET /version`` — версия и окружение для разбора инцидентов.

Наружу не отдаются ни DSN, ни тексты ошибок драйверов: только имя зависимости,
флаг и тип исключения.
"""

from __future__ import annotations

import sys

from fastapi import APIRouter, Response, status
from sqlalchemy import text

from hirebot import __version__
from hirebot.api.deps import ContainerDep, SettingsDep
from hirebot.api.schemas import ComponentHealth, HealthResponse, ReadyResponse, VersionResponse
from hirebot.container import Container
from hirebot.domain.enums import ConversationState
from hirebot.logging_setup import get_logger

logger = get_logger(__name__)

#: Версия интерпретатора — часть ответа ``/version``.
_PYTHON_VERSION = sys.version.split()[0]

router = APIRouter(tags=["health"])


async def check_database(container: Container) -> ComponentHealth:
    """Проверить доступность БД одним дешёвым запросом.

    Если у Unit of Work есть сессия — выполняем ``SELECT 1`` (это проверяет
    соединение, но не схему). Иначе пробуем репозиторий: тогда успех означает,
    что доступны и соединение, и таблицы.
    """
    try:
        async with container.uow_factory() as uow:
            execute = getattr(getattr(uow, "session", None), "execute", None)
            if callable(execute):
                await execute(text("SELECT 1"))
            else:
                await uow.conversations.list_by_state((ConversationState.NEW,), limit=1)
    except Exception as exc:
        logger.warning("readiness_db_failed", error_type=type(exc).__name__)
        return ComponentHealth(name="database", ok=False, detail=type(exc).__name__)
    return ComponentHealth(name="database", ok=True)


async def check_channels(container: Container) -> list[ComponentHealth]:
    """Проверить включённые каналы их собственным ``healthcheck``."""
    results: list[ComponentHealth] = []
    for channel, adapter in container.channels.items():
        try:
            ok = bool(await adapter.healthcheck())
        except Exception as exc:
            logger.warning(
                "readiness_channel_failed",
                channel=channel.value,
                error_type=type(exc).__name__,
            )
            results.append(
                ComponentHealth(name=f"channel:{channel.value}", ok=False, detail=type(exc).__name__)
            )
            continue
        results.append(ComponentHealth(name=f"channel:{channel.value}", ok=ok))
    return results


@router.get("/health", response_model=HealthResponse, summary="Liveness: процесс жив")
async def health() -> HealthResponse:
    """Ответить, что процесс жив. Внешние зависимости не проверяются."""
    return HealthResponse(version=__version__)


@router.get("/version", response_model=VersionResponse, summary="Версия и окружение")
async def version(settings: SettingsDep) -> VersionResponse:
    """Версия сборки, окружение и версия интерпретатора."""
    return VersionResponse(version=__version__, env=settings.env, python=_PYTHON_VERSION)


@router.get(
    "/health/ready",
    response_model=ReadyResponse,
    summary="Readiness: БД и каналы",
    responses={503: {"description": "Сервис не готов принимать трафик"}},
)
async def ready(container: ContainerDep, response: Response) -> ReadyResponse:
    """Проверить БД и каналы; ``503`` — если трафик принимать нельзя.

    Готовность = БД отвечает и хотя бы один включённый канал жив. Если каналов
    не включено вовсе, сервис считается готовым: это валидная конфигурация
    (например, демо на fake-адаптерах без внешних площадок).
    """
    database = await check_database(container)
    channels = await check_channels(container)
    channels_ok = not channels or any(channel.ok for channel in channels)
    is_ready = database.ok and channels_ok
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return ReadyResponse(
        status="ready" if is_ready else "degraded",
        database=database,
        channels=channels,
        queue_backend=container.settings.queue.backend,
        llm_provider=container.settings.llm.provider,
        calendar_configured=container.calendar is not None,
    )


__all__ = ["check_channels", "check_database", "router"]
