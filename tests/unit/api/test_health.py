"""Проверки ``/health``, ``/health/ready`` и ``/version``."""

from __future__ import annotations

from api_support import FakeChannel, FakeStore, asgi_client, make_container, make_settings
from httpx import AsyncClient

from hirebot.api.app import MAX_REQUEST_BODY_BYTES, create_app
from hirebot.domain.enums import Channel


async def test_health_answers_ok(client: AsyncClient) -> None:
    """Liveness отвечает 200 и версией сборки."""
    response = await client.get("/health")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["service"] == "hirebot"
    assert body["version"]


async def test_health_does_not_require_container() -> None:
    """Liveness не зависит от контейнера: его можно дёргать до готовности БД."""
    async with asgi_client(create_app()) as client:
        response = await client.get("/health")

    assert response.status_code == 200


async def test_version_returns_env(client: AsyncClient) -> None:
    """``/version`` показывает окружение: нужно для разбора инцидентов."""
    response = await client.get("/version")

    assert response.status_code == 200
    body = response.json()
    assert body["env"] == "test"
    assert body["name"] == "hirebot"
    assert body["python"]


async def test_ready_reports_database_and_channels(client: AsyncClient) -> None:
    """Readiness перечисляет проверенные зависимости."""
    response = await client.get("/health/ready")

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ready"
    assert body["database"]["ok"] is True
    assert [channel["name"] for channel in body["channels"]] == ["channel:hh"]
    assert body["queue_backend"] == "inline"
    assert body["llm_provider"] == "fake"
    assert body["calendar_configured"] is False


async def test_ready_is_503_when_database_is_down(store: FakeStore) -> None:
    """Недоступная БД — сервис не готов принимать трафик."""
    store.broken = True
    app = create_app(container=make_container(make_settings(), store=store))
    async with asgi_client(app) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "degraded"
    assert body["database"]["ok"] is False
    # Наружу — только тип исключения: ни DSN, ни трассировки.
    assert "RuntimeError" in (body["database"]["detail"] or "")


async def test_ready_is_503_when_all_channels_are_unhealthy() -> None:
    """Если все включённые каналы не отвечают — трафик принимать нельзя."""
    container = make_container(make_settings(), channels={Channel.HH: FakeChannel(Channel.HH, healthy=False)})
    async with asgi_client(create_app(container=container)) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 503
    assert response.json()["channels"][0]["ok"] is False


async def test_ready_tolerates_one_broken_channel() -> None:
    """Частичная деградация: один живой канал из двух — сервис готов."""
    container = make_container(
        make_settings(),
        channels={
            Channel.HH: FakeChannel(Channel.HH, healthy=True),
            Channel.AVITO: FakeChannel(Channel.AVITO, healthy=False),
        },
    )
    async with asgi_client(create_app(container=container)) as client:
        response = await client.get("/health/ready")

    assert response.status_code == 200
    assert response.json()["status"] == "ready"


async def test_response_carries_correlation_id(client: AsyncClient) -> None:
    """Сквозной идентификатор: по нему связываются логи запроса и задач."""
    response = await client.get("/health")

    assert response.headers["X-Correlation-ID"].startswith("req-")


async def test_correlation_id_is_reused_from_header(client: AsyncClient) -> None:
    """Внешний correlation id не перезаписывается своим."""
    response = await client.get("/health", headers={"X-Correlation-ID": "trace-42"})

    assert response.headers["X-Correlation-ID"] == "trace-42"


async def test_large_request_body_is_rejected(client: AsyncClient) -> None:
    """Тело больше лимита отклоняется с 413 и единым форматом ошибки."""
    oversized = b"x" * (MAX_REQUEST_BODY_BYTES + 1)

    response = await client.post("/webhooks/hh", content=oversized)

    assert response.status_code == 413
    assert response.json()["code"] == "payload_too_large"
