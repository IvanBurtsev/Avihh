"""Метрики Prometheus: HTTP, воронка, скрининг, сообщения, LLM, календарь, outbox.

Зачем отдельный пакет
---------------------
Метрики — это **контракт с дежурным**, а не отладочный вывод. Поэтому здесь
фиксированы три правила, которые важнее удобства:

1. **Никаких персональных данных и идентификаторов в labels.** Значения меток —
   только фиксированные перечисления (канал, состояние диалога, вердикт, тип
   сообщения, тип ошибки). Опечатка или новое значение из кода не создаёт новую
   серию: неизвестное значение превращается в ``unknown`` (см. ``_normalize``).
2. **Маршрут в метке — шаблон, а не URL.** ``/admin/conversations/{conversation_id}``,
   а не ``/admin/conversations/7c1f...``. Иначе кардинальность растёт вместе с
   числом кандидатов, и Prometheus (а с ним и Grafana) ложится.
3. **Сбой учёта не ломает запрос.** Все операции записи в метрики обёрнуты так,
   что исключение библиотеки не выходит наружу.

Метрики не создаются на импорте модуля: ``setup_metrics()`` вызывается на старте
приложения (в ``lifespan``), поэтому импорт модуля остаётся дешёвым, а тесты
могут пересоздавать набор метрик в каждом тесте.

Полный список метрик — в ``docs/RUNBOOK.md`` (раздел «Метрики») и в отчёте о
внедрении.
"""

from __future__ import annotations

import math
import threading
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from prometheus_client import (
    CONTENT_TYPE_LATEST,
    CollectorRegistry,
    Counter,
    Gauge,
    Histogram,
    generate_latest,
)
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from hirebot.domain.enums import (
    Channel,
    ConversationState,
    MessageKind,
    OutboxStatus,
    ScreeningVerdict,
)
from hirebot.domain.errors import DomainError
from hirebot.logging_setup import get_logger

logger = get_logger(__name__)

#: Префикс всех метрик приложения.
NAMESPACE: Final = "hirebot"

#: Значение метки для того, что не удалось распознать.
UNKNOWN_LABEL: Final = "unknown"

#: Метка маршрута для запросов, не совпавших ни с одним шаблоном (404 и т.п.).
UNMATCHED_ROUTE: Final = "unmatched"

#: Пути, которые не учитываются в HTTP-метриках: сам скрейп не должен
#: попадать в статистику запросов (иначе гистограмма врёт на пустом сервисе).
DEFAULT_EXCLUDED_PATHS: Final[frozenset[str]] = frozenset({"/metrics"})

#: границы гистограммы длительности HTTP-запроса, секунды.
HTTP_DURATION_BUCKETS: Final = (
    0.005,
    0.01,
    0.025,
    0.05,
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
)

#: Границы гистограммы задержки LLM: модели отвечают секундами, не миллисекундами.
LLM_LATENCY_BUCKETS: Final = (0.1, 0.25, 0.5, 1.0, 2.0, 5.0, 10.0, 20.0, 30.0, 60.0, 120.0)

#: Границы гистограммы балла скрининга (шкала 0..100).
SCORE_BUCKETS: Final = (0.0, 10.0, 20.0, 30.0, 40.0, 50.0, 60.0, 70.0, 80.0, 90.0, 100.0)

#: Провайдеры LLM, которые проект знает по имени. Новые добавляются здесь
#: осознанно: это единственный способ не пустить в labels произвольную строку.
_LLM_PROVIDERS: Final[frozenset[str]] = frozenset({"fake", "openai_compat"})

#: Исходы вызова модели.
_LLM_OUTCOMES: Final[frozenset[str]] = frozenset({"success", "error"})

#: События кэша LLM.
_LLM_CACHE_EVENTS: Final[frozenset[str]] = frozenset({"hit", "miss"})

#: Действия с встречами, которые учитываются отдельно.
_CALENDAR_ACTIONS: Final[frozenset[str]] = frozenset({"created", "cancelled"})

#: Известные значения меток, выведенные из доменных перечислений: единственный
#: источник правды о допустимых значениях.
_CHANNELS: Final[frozenset[str]] = frozenset(channel.value for channel in Channel)
_CONVERSATION_STATES: Final[frozenset[str]] = frozenset(state.value for state in ConversationState)
_MESSAGE_KINDS: Final[frozenset[str]] = frozenset(kind.value for kind in MessageKind)
_SCREENING_VERDICTS: Final[frozenset[str]] = frozenset(verdict.value for verdict in ScreeningVerdict)
_OUTBOX_STATUSES: Final[frozenset[str]] = frozenset(status.value for status in OutboxStatus)


def _domain_error_names() -> frozenset[str]:
    """Имена классов доменных ошибок — допустимые значения метки ``error_type``.

    Список выводится из иерархии ``DomainError``, а не пишется руками: новая
    доменная ошибка не потребует правки метрик, а произвольная строка
    (например, текст исключения драйвера БД) в метку не попадёт.
    """
    names: set[str] = {DomainError.__name__}
    pending: list[type[DomainError]] = list(DomainError.__subclasses__())
    while pending:
        error_type = pending.pop()
        names.add(error_type.__name__)
        pending.extend(error_type.__subclasses__())
    return frozenset(names)


_DOMAIN_ERRORS: Final[frozenset[str]] = _domain_error_names()


@dataclass(slots=True)
class _MetricSet:
    """Все метрики приложения в одном реестре.

    Отдельный ``CollectorRegistry`` (а не глобальный) нужен, чтобы набор можно
    было пересоздать в тестах и чтобы метрики приложения не смешивались с
    метриками чужих библиотек, зарегистрированных на импорте.
    """

    registry: CollectorRegistry
    http_requests: Counter
    http_duration: Histogram
    responses_ingested: Counter
    conversations: Gauge
    screenings: Counter
    screening_verdicts: Counter
    screening_scores: Histogram
    messages_sent: Counter
    messages_failed: Counter
    llm_calls: Counter
    llm_cache_events: Counter
    llm_latency: Histogram
    calendar_events: Counter
    slot_conflicts: Counter
    outbox_entries: Gauge
    domain_errors: Counter


_metrics: _MetricSet | None = None

#: Блокировка ленивого создания набора метрик (первый запрос может прийти
#: одновременно в нескольких потоках пула).
_build_lock: Final = threading.Lock()

#: Сбор включён по умолчанию. Отключается ``setup_metrics(enabled=False)``,
#: например в тестах, где метрики не являются предметом проверки.
_enabled: bool = True


def _build_metrics() -> _MetricSet:
    """Создать реестр и все метрики приложения."""
    registry = CollectorRegistry(auto_describe=True)

    http_requests = Counter(
        f"{NAMESPACE}_http_requests_total",
        "HTTP-запросы API",
        labelnames=("method", "route", "status"),
        registry=registry,
    )
    http_duration = Histogram(
        f"{NAMESPACE}_http_request_duration_seconds",
        "Длительность HTTP-запроса",
        labelnames=("method", "route"),
        buckets=HTTP_DURATION_BUCKETS,
        registry=registry,
    )
    responses_ingested = Counter(
        f"{NAMESPACE}_responses_ingested_total",
        "Принятые отклики кандидатов",
        labelnames=("channel",),
        registry=registry,
    )
    conversations = Gauge(
        f"{NAMESPACE}_conversations",
        "Диалоги в каждом состоянии воронки",
        labelnames=("state",),
        registry=registry,
    )
    screenings = Counter(
        f"{NAMESPACE}_screenings_total",
        "Выполненные скрининги резюме",
        registry=registry,
    )
    screening_verdicts = Counter(
        f"{NAMESPACE}_screening_verdicts_total",
        "Вердикты скрининга",
        labelnames=("verdict",),
        registry=registry,
    )
    screening_scores = Histogram(
        f"{NAMESPACE}_screening_score",
        "Распределение балла скрининга (0..100)",
        buckets=SCORE_BUCKETS,
        registry=registry,
    )
    messages_sent = Counter(
        f"{NAMESPACE}_messages_sent_total",
        "Успешно отправленные сообщения кандидатам",
        labelnames=("channel", "kind"),
        registry=registry,
    )
    messages_failed = Counter(
        f"{NAMESPACE}_messages_failed_total",
        "Неудачные попытки отправки сообщений",
        labelnames=("channel", "kind"),
        registry=registry,
    )
    llm_calls = Counter(
        f"{NAMESPACE}_llm_calls_total",
        "Вызовы LLM-провайдера",
        labelnames=("provider", "outcome"),
        registry=registry,
    )
    llm_cache_events = Counter(
        f"{NAMESPACE}_llm_cache_events_total",
        "Попадания и промахи кэша ответов LLM",
        labelnames=("provider", "event"),
        registry=registry,
    )
    llm_latency = Histogram(
        f"{NAMESPACE}_llm_latency_seconds",
        "Задержка вызова LLM",
        labelnames=("provider",),
        buckets=LLM_LATENCY_BUCKETS,
        registry=registry,
    )
    calendar_events = Counter(
        f"{NAMESPACE}_calendar_events_total",
        "Операции с встречами в календаре",
        labelnames=("action",),
        registry=registry,
    )
    slot_conflicts = Counter(
        f"{NAMESPACE}_calendar_slot_conflicts_total",
        "Конфликты слотов при бронировании",
        registry=registry,
    )
    outbox_entries = Gauge(
        f"{NAMESPACE}_outbox_entries",
        "Записи outbox по статусам (размер очереди и задачи в DEAD)",
        labelnames=("status",),
        registry=registry,
    )
    domain_errors = Counter(
        f"{NAMESPACE}_domain_errors_total",
        "Доменные ошибки по типам",
        labelnames=("error_type",),
        registry=registry,
    )

    return _MetricSet(
        registry=registry,
        http_requests=http_requests,
        http_duration=http_duration,
        responses_ingested=responses_ingested,
        conversations=conversations,
        screenings=screenings,
        screening_verdicts=screening_verdicts,
        screening_scores=screening_scores,
        messages_sent=messages_sent,
        messages_failed=messages_failed,
        llm_calls=llm_calls,
        llm_cache_events=llm_cache_events,
        llm_latency=llm_latency,
        calendar_events=calendar_events,
        slot_conflicts=slot_conflicts,
        outbox_entries=outbox_entries,
        domain_errors=domain_errors,
    )


def setup_metrics(*, enabled: bool = True) -> None:
    """Включить (или выключить) сбор метрик. Идемпотентно.

    Вызывается один раз на старте приложения — в ``lifespan`` API и в
    ``on_startup`` воркера. Повторный вызов безопасен: набор метрик не
    пересоздаётся, накопленные значения сохраняются. Если ``setup_metrics`` не
    вызвали вовсе, набор создаётся при первом обращении к метрикам (лениво):
    забытая инициализация не должна превращать ``/metrics`` в пустоту.

    ``enabled=False`` полностью выключает учёт: хелперы становятся no-op, а
    ``GET /metrics`` отвечает ``503`` (см. ``hirebot.api.routes_metrics``).
    """
    global _enabled, _metrics
    _enabled = enabled
    if not enabled:
        _metrics = None
        return
    if _metrics is None:
        _metrics = _build_metrics()


def reset_metrics() -> None:
    """Сбросить набор метрик и включить сбор заново.

    Нужен тестам: значения счётчиков должны быть предсказуемыми в каждом тесте.
    В рабочем процессе вызывать не следует — накопленные значения будут
    потеряны (и Prometheus примет скачок ``counter`` за рост).
    """
    global _metrics
    _metrics = None
    setup_metrics()


def metrics_enabled() -> bool:
    """Включён ли сбор метрик (``False`` — учёт отключён явно)."""
    return _enabled


def metrics_endpoint() -> tuple[bytes, str]:
    """Собрать ответ ``GET /metrics``: ``(payload, content_type)``.

    Если ``setup_metrics()`` ещё не вызывали, набор создаётся лениво: эндпоинт
    не должен зависеть от порядка инициализации в ``lifespan``.
    """
    metrics = _current()
    if metrics is None:
        return (b"# hirebot metrics disabled\n", CONTENT_TYPE_LATEST)
    return (generate_latest(metrics.registry), CONTENT_TYPE_LATEST)


def start_worker_metrics_server(port: int, *, address: str = "127.0.0.1") -> bool:
    """Поднять HTTP-сервер метрик внутри процесса воркера.

    Зачем: часть метрик — счётчики событий (скрининги, отправки, вызовы LLM), и
    они видны только в том процессе, где произошли. Основная работа идёт в
    воркере, поэтому без своего эндпоинта Prometheus видел бы «нули».

    Возвращает ``True``, если сервер запущен. Ошибки не пробрасываются: не
    поднявшийся порт метрик не должен останавливать обработку кандидатов.
    """
    current = _current()
    if current is None:
        return False
    try:
        from prometheus_client import start_http_server

        start_http_server(port, addr=address, registry=current.registry)
    except Exception as exc:
        logger.warning("metrics_server_failed", port=port, error=type(exc).__name__)
        return False
    logger.info("metrics_server_started", port=port, address=address)
    return True


def _current() -> _MetricSet | None:
    """Текущий набор метрик или ``None``, если учёт выключен.

    Набор создаётся при первом обращении (под блокировкой: первый запрос может
    прийти одновременно в нескольких потоках пула).
    """
    global _metrics
    if not _enabled:
        return None
    metrics = _metrics
    if metrics is None:
        with _build_lock:
            if _metrics is None:
                _metrics = _build_metrics()
            metrics = _metrics
    return metrics


def _normalize(value: object, allowed: frozenset[str]) -> str:
    """Привести значение метки к известному или к ``unknown``.

    Доменные перечисления наследуются от ``str``, поэтому и ``Channel.HH``, и
    строка ``"hh"`` превращаются в одно и то же значение метки.
    """
    text = str(value) if isinstance(value, str) else UNKNOWN_LABEL
    return text if text in allowed else UNKNOWN_LABEL


def _safe_observe(histogram: Histogram, value: float) -> None:
    """Записать наблюдение, не падая на ``NaN``/бесконечности."""
    if not math.isfinite(value):
        return
    histogram.observe(value)


def _route_template(scope: Scope) -> str:
    """Шаблон маршрута для метки ``route``.

    ``scope["route"]`` выставляет маршрутизатор Starlette. Для 404 и для
    несовпадения метода там лежит частичное совпадение (callable), у которого
    нет ``path``: такие запросы попадают в ``unmatched`` — фактический URL в
    метку не попадает никогда.
    """
    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path and len(path) <= 200:
        return path
    return UNMATCHED_ROUTE


def record_http_request(*, method: str, route: str, status: int, seconds: float) -> None:
    """Учесть один HTTP-запрос (вызывается middleware, но может и вручную)."""
    metrics = _current()
    if metrics is None:
        return
    try:
        metrics.http_requests.labels(method=method, route=route, status=str(status)).inc()
        metrics.http_duration.labels(method=method, route=route).observe(max(0.0, seconds))
    except Exception as exc:  # pragma: no cover - защита от сбоя библиотеки
        logger.warning("metrics_http_record_failed", error_type=type(exc).__name__)


class HttpMetricsMiddleware:
    """Чистый ASGI-middleware: считает HTTP-запросы и их длительность.

    Почему не ``BaseHTTPMiddleware``: он выполняет приложение в отдельной
    задаче, из-за чего контекстные переменные (correlation id) теряются.
    Тот же приём используется в ``RequestContextMiddleware``.

    Middleware **не** перехватывает исключения: они продолжают идти наверх в
    обработчики, а метрика в этом случае получает статус ``500``.
    """

    def __init__(
        self,
        app: ASGIApp,
        *,
        exclude_paths: frozenset[str] = DEFAULT_EXCLUDED_PATHS,
    ) -> None:
        self.app = app
        self.exclude_paths = exclude_paths

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http" or not metrics_enabled():
            await self.app(scope, receive, send)
            return

        path = scope.get("path", "")
        if path in self.exclude_paths:
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        status_code = 500

        async def send_with_metrics(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, send_with_metrics)
        finally:
            record_http_request(
                method=str(scope.get("method", "GET")),
                route=_route_template(scope),
                status=status_code,
                seconds=time.perf_counter() - started,
            )


def record_response_ingested(channel: str) -> None:
    """Учесть принятый отклик кандидата (по каналу)."""
    metrics = _current()
    if metrics is None:
        return
    metrics.responses_ingested.labels(channel=_normalize(channel, _CHANNELS)).inc()


def observe_conversations(states: Mapping[str, int]) -> None:
    """Обновить gauge числа диалогов в каждом состоянии воронки.

    Состояния, которых нет в ``states``, сбрасываются в ноль: иначе после
    завершения всех диалогов в каком-то состоянии график показывал бы
    «застрявшие» значения.
    """
    metrics = _current()
    if metrics is None:
        return
    for state in _CONVERSATION_STATES:
        raw = states.get(state, 0)
        try:
            value = max(0, int(raw))
        except (TypeError, ValueError):
            value = 0
        metrics.conversations.labels(state=state).set(value)


def record_screening(verdict: str, score: float) -> None:
    """Учесть один скрининг: вердикт и балл.

    Балл вне шкалы ``0..100`` обрезается, ``NaN``/бесконечность в гистограмму
    не попадают: иначе распределение оценок перестаёт быть читаемым.
    """
    metrics = _current()
    if metrics is None:
        return
    metrics.screenings.inc()
    metrics.screening_verdicts.labels(verdict=_normalize(verdict, _SCREENING_VERDICTS)).inc()
    try:
        numeric = float(score)
    except (TypeError, ValueError):
        return
    if not math.isfinite(numeric):
        return
    _safe_observe(metrics.screening_scores, min(100.0, max(0.0, numeric)))


def record_message_sent(channel: str, kind: str) -> None:
    """Учесть отправленное сообщение кандидату."""
    metrics = _current()
    if metrics is None:
        return
    metrics.messages_sent.labels(
        channel=_normalize(channel, _CHANNELS),
        kind=_normalize(kind, _MESSAGE_KINDS),
    ).inc()


def record_message_failed(channel: str, kind: str) -> None:
    """Учесть неудачную попытку отправки сообщения."""
    metrics = _current()
    if metrics is None:
        return
    metrics.messages_failed.labels(
        channel=_normalize(channel, _CHANNELS),
        kind=_normalize(kind, _MESSAGE_KINDS),
    ).inc()


def record_llm_call(provider: str, outcome: str, seconds: float, cached: bool) -> None:
    """Учесть вызов LLM: провайдер, исход, задержку и попадание в кэш.

    ``cached=True`` означает, что ответ взят из кэша: его задержка не имеет
    отношения к провайдеру, поэтому гистограмма задержки обновляется только
    для настоящих вызовов.
    """
    metrics = _current()
    if metrics is None:
        return
    provider_label = _normalize(provider, _LLM_PROVIDERS)
    metrics.llm_calls.labels(
        provider=provider_label,
        outcome=_normalize(outcome, _LLM_OUTCOMES),
    ).inc()
    metrics.llm_cache_events.labels(
        provider=provider_label,
        event="hit" if cached else "miss",
    ).inc()
    if not cached:
        try:
            latency = float(seconds)
        except (TypeError, ValueError):
            return
        _safe_observe(metrics.llm_latency.labels(provider=provider_label), max(0.0, latency))


def record_calendar_event(action: str) -> None:
    """Учесть операцию с встречей: ``created`` или ``cancelled``."""
    metrics = _current()
    if metrics is None:
        return
    metrics.calendar_events.labels(action=_normalize(action, _CALENDAR_ACTIONS)).inc()


def record_slot_conflict() -> None:
    """Учесть конфликт слотов (слот уже занят)."""
    metrics = _current()
    if metrics is None:
        return
    metrics.slot_conflicts.inc()


def record_outbox(status: str, size: int | None = None) -> None:
    """Обновить размер outbox по статусу.

    ``status="dead"`` — задачи, требующие человека (см. ``docs/RUNBOOK.md``).
    ``size=None`` означает «размер неизвестен»: серия инициализируется нулём,
    чтобы на графике не было пропусков. Если размер известен — передавайте его.
    """
    metrics = _current()
    if metrics is None:
        return
    gauge = metrics.outbox_entries.labels(status=_normalize(status, _OUTBOX_STATUSES))
    if size is None:
        gauge.set(0)
        return
    try:
        gauge.set(max(0, int(size)))
    except (TypeError, ValueError):
        return


def record_domain_error(error: BaseException | str) -> None:
    """Учесть доменную ошибку по типу.

    Принимается и объект исключения, и его имя: сервисы ловят ошибки по типу и
    иногда передают имя класса строкой. Значение метки — только из иерархии
    ``DomainError``; всё остальное считается ``unknown``.
    """
    metrics = _current()
    if metrics is None:
        return
    error_name = type(error).__name__ if isinstance(error, BaseException) else str(error)
    metrics.domain_errors.labels(error_type=_normalize(error_name, _DOMAIN_ERRORS)).inc()


__all__ = [
    "DEFAULT_EXCLUDED_PATHS",
    "HTTP_DURATION_BUCKETS",
    "LLM_LATENCY_BUCKETS",
    "NAMESPACE",
    "SCORE_BUCKETS",
    "UNKNOWN_LABEL",
    "UNMATCHED_ROUTE",
    "HttpMetricsMiddleware",
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
    "reset_metrics",
    "setup_metrics",
    "start_worker_metrics_server",
]
