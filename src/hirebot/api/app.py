"""Сборка FastAPI-приложения.

Два решения, которые здесь важно понимать:

1. **Контейнер создаётся лениво, в ``lifespan``.** Импорт ``hirebot.api.app`` не
   поднимает БД, Redis и HTTP-клиенты — это позволяет тестам и линтерам
   импортировать модуль, а тестам ещё и подсунуть свой контейнер
   (``create_app(container=...)``).
2. **Middleware — чистый ASGI, а не ``BaseHTTPMiddleware``.** Только так
   ``correlation_id`` гарантированно живёт в том же контексте, что и обработчик
   запроса (``BaseHTTPMiddleware`` выполняет приложение в отдельной задаче).

Ограничения безопасности, реализованные здесь:

* тело запроса больше ``MAX_REQUEST_BODY_BYTES`` отклоняется с ``413`` (вебхуки
  и ручные сообщения — единственные места, куда приходит внешний ввод);
* каждый ответ несёт ``X-Correlation-ID`` — по нему связываются логи запроса и
  фоновых задач, которые этот запрос породил.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Final
from uuid import uuid4

from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hirebot import __version__
from hirebot.api.errors import error_response, install_exception_handlers, new_error_id
from hirebot.api.routes_admin import router as admin_router
from hirebot.api.routes_health import router as health_router
from hirebot.api.routes_metrics import router as metrics_router
from hirebot.api.routes_ui import router as ui_router
from hirebot.api.routes_webhooks import router as webhooks_router
from hirebot.config import Settings, get_settings
from hirebot.container import Container, build_container
from hirebot.logging_setup import bind_correlation_id, clear_context, configure_logging, get_logger
from hirebot.observability.metrics import HttpMetricsMiddleware, setup_metrics

logger = get_logger(__name__)

#: Статика UI (CSS и JS). Собирается без Node: файлы лежат рядом с шаблонами.
STATIC_DIR: Final = Path(__file__).parent / "static"

#: Заголовок сквозного идентификатора запроса.
CORRELATION_HEADER: Final = "X-Correlation-ID"

#: Верхняя граница тела запроса: 1 МиБ. Больше в API прийти не должно.
MAX_REQUEST_BODY_BYTES: Final = 1_048_576


def _settings_of(container: Container | None) -> Settings:
    """Настройки: из готового контейнера, иначе из окружения."""
    return container.settings if container is not None else get_settings()


@asynccontextmanager
async def lifespan(app: FastAPI) -> AsyncIterator[None]:
    """Поднять контейнер на старте и закрыть ресурсы на остановке.

    Контейнер, подложенный извне (тесты, встроенные сценарии), не
    пересобирается и не закрывается: его жизненным циклом управляет владелец.
    """
    app_container: Container | None = getattr(app.state, "container", None)
    owns_container = app_container is None
    settings = _settings_of(app_container)

    configure_logging(level=settings.log_level, json_output=settings.log_json)
    # Метрики включаются до сборки контейнера: middleware учитывает в том числе
    # запросы, завершившиеся ошибкой старта.
    setup_metrics(enabled=True)
    if app_container is None:
        # Интерфейс и API должны открываться на чистой машине: для локальной
        # файловой SQLite схема создаётся сама. Для PostgreSQL это no-op —
        # там схему создаёт Alembic, и забытые миграции не маскируются.
        from hirebot.infra.db.session import ensure_local_schema

        created = await ensure_local_schema(settings)
        if created:
            logger.info("db_schema_autocreated", hint="локальный режим, SQLite")

        app_container = await build_container(settings)
        app.state.container = app_container
        await app_container.queue.start()
        logger.info("api_startup", env=settings.env, queue=settings.queue.backend)

    try:
        yield
    finally:
        if owns_container and app_container is not None:
            await app_container.aclose()
            logger.info("api_shutdown")


class RequestContextMiddleware:
    """Correlation id и ограничение размера тела запроса.

    Реализован как чистый ASGI-middleware: контекстные переменные, привязанные
    здесь, видны обработчику, потому что всё выполняется в одной задаче.
    """

    def __init__(self, app: ASGIApp, *, max_body_bytes: int = MAX_REQUEST_BODY_BYTES) -> None:
        self.app = app
        self.max_body_bytes = max_body_bytes

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        correlation_id = headers.get(CORRELATION_HEADER) or f"req-{uuid4().hex[:12]}"
        bind_correlation_id(correlation_id)

        async def send_with_correlation(message: Message) -> None:
            if message["type"] == "http.response.start":
                MutableHeaders(scope=message)[CORRELATION_HEADER] = correlation_id
            await send(message)

        try:
            declared_length = headers.get("content-length")
            if (
                declared_length is not None
                and declared_length.isdigit()
                and int(declared_length) > self.max_body_bytes
            ):
                logger.warning(
                    "request_too_large",
                    path=scope.get("path", ""),
                    content_length=int(declared_length),
                )
                response = error_response(
                    status_code=413,
                    error_id=new_error_id(),
                    code="payload_too_large",
                    message="Тело запроса слишком большое",
                )
                await response(scope, receive, send_with_correlation)
                return
            await self.app(scope, receive, send_with_correlation)
        finally:
            clear_context()


def create_app(container: Container | None = None) -> FastAPI:
    """Собрать приложение.

    ``container`` передаётся тестами и встроенными запусками; если он не задан,
    контейнер будет собран в ``lifespan`` по настройкам окружения.
    """
    app = FastAPI(
        title="hirebot API",
        description=(
            "AI-рекрутер: приём откликов с площадок, скрининг резюме, диалог с кандидатом "
            "и запись на собеседование."
        ),
        version=__version__,
        lifespan=lifespan,
    )
    if container is not None:
        app.state.container = container

    app.add_middleware(RequestContextMiddleware)
    # Метрики — самым внешним middleware: иначе запросы, отклонённые по размеру
    # тела (413), не попадали бы в статистику, хотя это реальные инциденты.
    app.add_middleware(HttpMetricsMiddleware)
    install_exception_handlers(app)

    app.include_router(health_router)
    app.include_router(metrics_router)
    app.include_router(webhooks_router)
    app.include_router(admin_router)
    app.include_router(ui_router)

    # Статика UI: один CSS и один JS, без сборки фронтенда.
    app.mount("/ui/static", StaticFiles(directory=str(STATIC_DIR)), name="ui-static")

    return app


#: Приложение для ASGI-сервера (``uvicorn hirebot.api.app:app``).
#: Контейнер появится в ``lifespan``: на импорте никакого I/O не происходит.
app = create_app()


__all__ = [
    "CORRELATION_HEADER",
    "MAX_REQUEST_BODY_BYTES",
    "STATIC_DIR",
    "RequestContextMiddleware",
    "app",
    "create_app",
    "lifespan",
]
