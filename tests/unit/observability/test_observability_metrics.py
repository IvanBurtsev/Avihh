"""Тесты метрик, ``HttpMetricsMiddleware`` и ``GET /metrics``.

Проверяется **контракт с Prometheus**: имена метрик, наборы меток и защита от
кардинальности. Внутреннее устройство реестра тесты не трогают — читают ту же
экспозицию, которую увидит Prometheus.
"""

from __future__ import annotations

import pytest
from observability_support import (
    build_metrics_app,
    client_for,
    make_container,
    make_settings,
    metric_samples,
    sample_value,
)

from hirebot.domain.errors import AuthExpired, RateLimited
from hirebot.observability.metrics import (
    HttpMetricsMiddleware,
    metrics_enabled,
    metrics_endpoint,
    observe_conversations,
    record_calendar_event,
    record_domain_error,
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


def exposition() -> str:
    """Текущая выгрузка метрик в виде текста."""
    payload, _content_type = metrics_endpoint()
    return payload.decode("utf-8")


# --------------------------------------------------------------------------- #
# Инициализация и формат ответа
# --------------------------------------------------------------------------- #


def test_setup_metrics_is_idempotent() -> None:
    """Повторный ``setup_metrics`` не пересоздаёт набор и не падает."""
    record_response_ingested("hh")
    setup_metrics()
    setup_metrics(enabled=True)

    assert sample_value(exposition(), "hirebot_responses_ingested_total", channel="hh") == 1.0


def test_metrics_endpoint_returns_prometheus_text() -> None:
    """Эндпоинт отдаёт ``text/plain`` версии Prometheus и HELP/TYPE-строки."""
    payload, content_type = metrics_endpoint()

    assert isinstance(payload, bytes)
    assert content_type.startswith("text/plain")
    text = payload.decode("utf-8")
    assert "# HELP hirebot_http_requests_total" in text
    assert "# TYPE hirebot_http_requests_total counter" in text
    assert "# TYPE hirebot_screening_score histogram" in text


def test_enable_after_disable_resumes_collection() -> None:
    """После повторного включения учёт продолжается с чистого набора."""
    setup_metrics(enabled=False)
    record_response_ingested("hh")
    setup_metrics(enabled=True)

    record_response_ingested("hh")

    assert metrics_enabled() is True
    assert sample_value(exposition(), "hirebot_responses_ingested_total", channel="hh") == 1.0


def test_disable_stops_collection() -> None:
    """``setup_metrics(enabled=False)`` превращает учёт в no-op."""
    setup_metrics(enabled=False)

    record_response_ingested("hh")
    payload, _ = metrics_endpoint()

    assert metrics_enabled() is False
    assert b"# hirebot metrics disabled" in payload
    assert b"hirebot_responses_ingested_total" not in payload


# --------------------------------------------------------------------------- #
# Бизнес-метрики
# --------------------------------------------------------------------------- #


def test_response_ingested_counts_per_channel() -> None:
    """Отклики считаются по каналам."""
    record_response_ingested("hh")
    record_response_ingested("hh")
    record_response_ingested("avito")

    text = exposition()
    assert sample_value(text, "hirebot_responses_ingested_total", channel="hh") == 2.0
    assert sample_value(text, "hirebot_responses_ingested_total", channel="avito") == 1.0


def test_unknown_label_value_is_collapsed() -> None:
    """Произвольная строка в канале не создаёт новую серию."""
    record_response_ingested("hh-vacancy-42-candidate-ivan")

    text = exposition()
    assert sample_value(text, "hirebot_responses_ingested_total", channel="unknown") == 1.0
    assert '"hh-vacancy-42-candidate-ivan"' not in text


def test_observe_conversations_resets_absent_states() -> None:
    """Состояния, которых нет в снимке, обнуляются."""
    observe_conversations({"new": 3, "booked": 2})
    observe_conversations({"new": 1})

    text = exposition()
    assert sample_value(text, "hirebot_conversations", state="new") == 1.0
    assert sample_value(text, "hirebot_conversations", state="booked") == 0.0
    # Полный набор состояний присутствует всегда: графики не «прыгают».
    assert sample_value(text, "hirebot_conversations", state="human_review") == 0.0


def test_record_screening_tracks_verdict_and_score() -> None:
    """Скрининг: общий счётчик, вердикт и балл в гистограмме."""
    record_screening("match", 74.0)
    record_screening("reject", 12.5)

    text = exposition()
    assert sample_value(text, "hirebot_screenings_total") == 2.0
    assert sample_value(text, "hirebot_screening_verdicts_total", verdict="match") == 1.0
    assert sample_value(text, "hirebot_screening_verdicts_total", verdict="reject") == 1.0
    assert sample_value(text, "hirebot_screening_score_count") == 2.0


def test_screening_score_is_clamped_and_survives_nonsense() -> None:
    """Балл вне 0..100 обрезается, ``NaN`` не попадает в гистограмму."""
    record_screening("match", 137.0)
    record_screening("match", -5.0)
    record_screening("match", float("nan"))

    text = exposition()
    assert sample_value(text, "hirebot_screening_score_count") == 2.0
    assert sample_value(text, "hirebot_screening_score_sum") == 100.0


def test_messages_sent_and_failed_are_separate() -> None:
    """Успешные и неуспешные отправки не смешиваются."""
    record_message_sent("hh", "greeting")
    record_message_sent("hh", "greeting")
    record_message_failed("hh", "greeting")
    record_message_failed("avito", "custom_kind")

    text = exposition()
    assert sample_value(text, "hirebot_messages_sent_total", channel="hh", kind="greeting") == 2.0
    assert sample_value(text, "hirebot_messages_failed_total", channel="hh", kind="greeting") == 1.0
    assert sample_value(text, "hirebot_messages_failed_total", channel="avito", kind="unknown") == 1.0


def test_record_llm_call_tracks_outcome_cache_and_latency() -> None:
    """LLM: вызовы, кэш и задержка (для кэша задержка не наблюдается)."""
    record_llm_call("openai_compat", "success", 1.5, cached=False)
    record_llm_call("openai_compat", "error", 30.0, cached=False)
    record_llm_call("openai_compat", "success", 0.001, cached=True)

    text = exposition()
    assert sample_value(text, "hirebot_llm_calls_total", provider="openai_compat", outcome="success") == 2.0
    assert sample_value(text, "hirebot_llm_calls_total", provider="openai_compat", outcome="error") == 1.0
    assert sample_value(text, "hirebot_llm_cache_events_total", provider="openai_compat", event="hit") == 1.0
    assert sample_value(text, "hirebot_llm_cache_events_total", provider="openai_compat", event="miss") == 2.0
    # Задержка наблюдается только для настоящих вызовов: 1.5 и 30.0.
    assert sample_value(text, "hirebot_llm_latency_seconds_count", provider="openai_compat") == 2.0


def test_record_llm_call_unknown_provider_is_collapsed() -> None:
    """Незнакомый провайдер не может размножить серии."""
    record_llm_call("some-vendor/model-2026-01", "success", 1.0, cached=False)

    text = exposition()
    assert sample_value(text, "hirebot_llm_calls_total", provider="unknown", outcome="success") == 1.0
    assert "some-vendor" not in text


def test_calendar_and_slot_conflicts() -> None:
    """Встречи и конфликты слотов считаются раздельно."""
    record_calendar_event("created")
    record_calendar_event("cancelled")
    record_calendar_event("something_else")
    record_slot_conflict()

    text = exposition()
    assert sample_value(text, "hirebot_calendar_events_total", action="created") == 1.0
    assert sample_value(text, "hirebot_calendar_events_total", action="cancelled") == 1.0
    assert sample_value(text, "hirebot_calendar_events_total", action="unknown") == 1.0
    assert sample_value(text, "hirebot_calendar_slot_conflicts_total") == 1.0


def test_record_outbox_sets_gauge_per_status() -> None:
    """Размер очереди и задачи в DEAD видны отдельными сериями."""
    record_outbox("pending", 12)
    record_outbox("dead", 2)
    record_outbox("sent", None)

    text = exposition()
    assert sample_value(text, "hirebot_outbox_entries", status="pending") == 12.0
    assert sample_value(text, "hirebot_outbox_entries", status="dead") == 2.0
    assert sample_value(text, "hirebot_outbox_entries", status="sent") == 0.0


def test_record_outbox_ignores_bad_size() -> None:
    """Отрицательный размер обрезается нулём, мусор не затирает значение."""
    record_outbox("pending", -3)
    record_outbox("dead", 4)
    record_outbox("dead", "много")  # type: ignore[arg-type]

    text = exposition()
    assert sample_value(text, "hirebot_outbox_entries", status="pending") == 0.0
    assert sample_value(text, "hirebot_outbox_entries", status="dead") == 4.0


def test_domain_errors_are_counted_by_type() -> None:
    """Ошибки считаются по типу доменного исключения."""
    record_domain_error(AuthExpired("токен истёк"))
    record_domain_error(RateLimited("429", retry_after_s=30))
    record_domain_error("RateLimited")

    text = exposition()
    assert sample_value(text, "hirebot_domain_errors_total", error_type="AuthExpired") == 1.0
    assert sample_value(text, "hirebot_domain_errors_total", error_type="RateLimited") == 2.0


def test_non_domain_error_text_does_not_leak_into_labels() -> None:
    """Строка не из иерархии ``DomainError`` не попадает в метку."""
    record_domain_error("OperationalError: password authentication failed for user hirebot")

    text = exposition()
    assert sample_value(text, "hirebot_domain_errors_total", error_type="unknown") == 1.0
    assert "password" not in text


# --------------------------------------------------------------------------- #
# Middleware
# --------------------------------------------------------------------------- #


async def test_middleware_counts_request_with_route_template() -> None:
    """В метке ``route`` — шаблон маршрута, а не фактический URL."""
    app = build_metrics_app(make_container(), with_router=False)
    async with client_for(app) as client:
        response = await client.get("/items/7c1f2b3a-4d5e-6f70-8192-a3b4c5d6e7f8")

    assert response.status_code == 200
    text = exposition()
    assert (
        sample_value(
            text, "hirebot_http_requests_total", method="GET", route="/items/{item_id}", status="200"
        )
        == 1.0
    )
    assert "7c1f2b3a" not in text
    assert sample_value(text, "hirebot_http_request_duration_seconds_count", route="/items/{item_id}") == 1.0


async def test_middleware_marks_unmatched_requests() -> None:
    """404 не создаёт серию на каждый встреченный URL."""
    app = build_metrics_app(make_container(), with_router=False)
    async with client_for(app) as client:
        first = await client.get("/no-such-page-1")
        second = await client.get("/no-such-page-2")

    assert first.status_code == 404
    assert second.status_code == 404
    text = exposition()
    assert (
        sample_value(text, "hirebot_http_requests_total", method="GET", route="unmatched", status="404")
        == 2.0
    )
    assert "no-such-page" not in text


async def test_middleware_does_not_break_requests_and_hides_metrics_endpoint() -> None:
    """Проксирование не меняет ответ, а сам скрейп в статистику не попадает."""
    container = make_container(make_settings(security={"webhook_secret": "s3cret"}))
    app = build_metrics_app(container)
    async with client_for(app) as client:
        response = await client.get("/metrics", headers={"X-API-Key": "s3cret"})

    assert response.status_code == 200
    text = exposition()
    assert all(
        sample_labels.get("route") != "/metrics" for _name, sample_labels, _value in metric_samples(text)
    )


async def test_middleware_counts_500_and_reraises_exception() -> None:
    """Упавший обработчик учитывается как 500, исключение идёт дальше."""

    async def failing_app(scope: object, receive: object, send: object) -> None:
        msg = "падение"
        raise RuntimeError(msg)

    middleware = HttpMetricsMiddleware(failing_app)  # type: ignore[arg-type]
    scope = {"type": "http", "path": "/x", "method": "GET", "headers": []}

    async def receive() -> dict[str, object]:
        return {"type": "http.request"}

    async def send(_message: object) -> None:
        return None

    with pytest.raises(RuntimeError):
        await middleware(scope, receive, send)  # type: ignore[arg-type]

    text = exposition()
    assert sample_value(text, "hirebot_http_requests_total", route="unmatched", status="500") == 1.0


async def test_middleware_passes_through_non_http_scopes() -> None:
    """Lifespan и websocket не считаются HTTP-запросами."""
    calls: list[str] = []

    async def app(scope: dict[str, object], receive: object, send: object) -> None:
        calls.append(str(scope["type"]))

    middleware = HttpMetricsMiddleware(app)  # type: ignore[arg-type]
    await middleware({"type": "lifespan"}, object, object)  # type: ignore[arg-type]

    assert calls == ["lifespan"]
    assert all(
        name != "hirebot_http_requests_total" for name, _labels, _value in metric_samples(exposition())
    )


async def test_middleware_is_transparent_when_disabled() -> None:
    """Выключенный сбор не мешает приложению и ничего не пишет."""
    setup_metrics(enabled=False)
    app = build_metrics_app(make_container(), with_router=False)
    async with client_for(app) as client:
        response = await client.get("/items/abc")

    assert response.status_code == 200
    assert metrics_enabled() is False


async def test_middleware_counts_500_when_handler_sends_nothing() -> None:
    """Если ответ так и не начался, запрос считается как 500."""

    async def app(scope: object, receive: object, send: object) -> None:
        # Обработчик ничего не отправил: статус остаётся 500 по умолчанию.
        return None

    middleware = HttpMetricsMiddleware(app)  # type: ignore[arg-type]
    await middleware(  # type: ignore[arg-type]
        {"type": "http", "path": "/y", "method": "POST", "headers": []},
        object,
        object,
    )

    text = exposition()
    assert sample_value(text, "hirebot_http_requests_total", method="POST", status="500") == 1.0


# --------------------------------------------------------------------------- #
# Роутер /metrics
# --------------------------------------------------------------------------- #


async def test_metrics_endpoint_returns_payload_with_key() -> None:
    """С корректным ключом ``/metrics`` отдаёт метрики."""
    container = make_container(make_settings(security={"webhook_secret": "s3cret"}))
    app = build_metrics_app(container)

    async with client_for(app) as client:
        response = await client.get("/metrics", headers={"X-API-Key": "s3cret"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/plain")
    assert response.headers["cache-control"] == "no-store"
    assert "hirebot_http_requests_total" in response.text


async def test_metrics_endpoint_requires_key() -> None:
    """Без ключа и с неверным ключом — 401: метрики не публичны."""
    container = make_container(make_settings(security={"webhook_secret": "s3cret"}))
    app = build_metrics_app(container)

    async with client_for(app) as client:
        without_key = await client.get("/metrics")
        wrong_key = await client.get("/metrics", headers={"X-API-Key": "wrong"})

    assert without_key.status_code == 401
    assert wrong_key.status_code == 401


async def test_metrics_endpoint_is_503_without_secret_outside_local() -> None:
    """В нелокальном окружении без секрета открытый доступ запрещён."""
    container = make_container(make_settings(env="staging", security={}))
    app = build_metrics_app(container)

    async with client_for(app) as client:
        response = await client.get("/metrics")

    assert response.status_code == 503


async def test_metrics_endpoint_is_503_when_collection_disabled() -> None:
    """Выключенный сбор честно сообщает о себе, а не отдаёт пустоту."""
    setup_metrics(enabled=False)
    container = make_container(make_settings(security={"webhook_secret": "s3cret"}))
    app = build_metrics_app(container)

    async with client_for(app) as client:
        response = await client.get("/metrics", headers={"X-API-Key": "s3cret"})

    assert response.status_code == 503
    assert "выключен" in response.text


async def test_metrics_endpoint_is_hidden_from_openapi() -> None:
    """Внутренний эндпоинт не публикуется в схеме API."""
    app = build_metrics_app(make_container(), with_middleware=False)

    async with client_for(app) as client:
        response = await client.get("/openapi.json")

    assert "/metrics" not in response.json()["paths"]


def test_reset_metrics_clears_counters() -> None:
    """Сброс набора обнуляет счётчики (нужно для повторяемости тестов)."""
    record_response_ingested("hh")
    reset_metrics()

    assert sample_value(exposition(), "hirebot_responses_ingested_total", channel="hh") is None
