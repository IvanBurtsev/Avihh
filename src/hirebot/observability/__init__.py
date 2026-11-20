"""Эксплуатационная обвязка: метрики Prometheus и расширенная готовность.

Пакет намеренно не зависит от ``hirebot.api``: его используют и API, и воркер,
и CLI. Обратная зависимость (``api`` → ``observability``) — нормальная.

Что внутри:

``metrics``
    Реестр метрик, ``HttpMetricsMiddleware``, хелперы учёта бизнес-событий и
    ``metrics_endpoint()`` для ``GET /metrics``. Все метки — фиксированные
    перечисления, маршрут берётся шаблоном (см. docstring модуля).

``health``
    Проверка зависимостей (БД, Redis, каналы, календарь, LLM) с таймаутами и
    структурным результатом ``ReadinessReport``. Подключается к существующему
    ``/health/ready``.

Типичная инициализация API (в ``lifespan``)::

    from hirebot.observability.metrics import setup_metrics

    setup_metrics(enabled=True)

и в ``create_app``::

    from hirebot.api.routes_metrics import router as metrics_router

    app.add_middleware(HttpMetricsMiddleware)
    app.include_router(metrics_router)
"""

from __future__ import annotations

from hirebot.observability.health import (
    CHANNEL_PREFIX,
    DEFAULT_CHECK_TIMEOUT_S,
    DependencyStatus,
    ReadinessReport,
    check_dependencies,
    redis_required,
)
from hirebot.observability.metrics import (
    DEFAULT_EXCLUDED_PATHS,
    NAMESPACE,
    UNKNOWN_LABEL,
    UNMATCHED_ROUTE,
    HttpMetricsMiddleware,
    metrics_enabled,
    metrics_endpoint,
    observe_conversations,
    record_calendar_event,
    record_domain_error,
    record_http_request,
    record_llm_call,
    record_message_failed,
    record_message_sent,
    record_outbox,
    record_response_ingested,
    record_screening,
    record_slot_conflict,
    reset_metrics,
    setup_metrics,
)

__all__ = [
    "CHANNEL_PREFIX",
    "DEFAULT_CHECK_TIMEOUT_S",
    "DEFAULT_EXCLUDED_PATHS",
    "NAMESPACE",
    "UNKNOWN_LABEL",
    "UNMATCHED_ROUTE",
    "DependencyStatus",
    "HttpMetricsMiddleware",
    "ReadinessReport",
    "check_dependencies",
    "metrics_enabled",
    "metrics_endpoint",
    "observe_conversations",
    "record_calendar_event",
    "record_domain_error",
    "record_http_request",
    "record_llm_call",
    "record_message_failed",
    "record_message_sent",
    "record_outbox",
    "record_response_ingested",
    "record_screening",
    "record_slot_conflict",
    "redis_required",
    "reset_metrics",
    "setup_metrics",
]
