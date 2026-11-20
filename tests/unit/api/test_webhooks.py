"""Тесты приёма вебхуков.

Проверяются три свойства, без которых вебхук опасен или бесполезен:

* подпись/секрет обязателен вне локального окружения;
* повтор доставки (at-least-once у площадок) не приводит к повторной обработке;
* HTTP-ответ быстрый: тяжёлая работа уходит в очередь.
"""

from __future__ import annotations

import hashlib
import hmac
from uuid import uuid4

from api_support import (
    Channel,
    FakeCache,
    FakeQueue,
    FakeStore,
    asgi_client,
    make_container,
    make_settings,
    seed_conversation,
)
from httpx import AsyncClient

from hirebot.api.app import create_app

SECRET = "topsecret"


def _sign(body: bytes, secret: str = SECRET) -> str:
    """Подпись тела так, как её считает площадка."""
    return hmac.new(secret.encode("utf-8"), body, hashlib.sha256).hexdigest()


def _app_with_secret(*, store: FakeStore, cache: FakeCache, queue: FakeQueue):
    settings = make_settings(webhook_secret=SECRET)
    return create_app(container=make_container(settings, store=store, cache=cache, queue=queue))


async def test_webhook_is_accepted_and_enqueues_polling(client: AsyncClient, queue: FakeQueue) -> None:
    """Уведомление без диалога превращается в задачу поллинга канала."""
    response = await client.post("/webhooks/hh", json={"event_id": "evt-1", "event": "new_response"})

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "accepted"
    assert body["channel"] == "hh"
    assert body["job"] == "poll_channel"
    assert body["duplicate"] is False
    assert queue.jobs == [
        {"job": "poll_channel", "payload": {"channel": "hh"}, "dedupe_key": "webhook:hh:evt-1"}
    ]


async def test_webhook_with_conversation_enqueues_processing(client: AsyncClient, queue: FakeQueue) -> None:
    """Если в событии есть диалог — ставим обработку именно его."""
    conversation = seed_conversation(FakeStore())

    response = await client.post(
        "/webhooks/hh",
        json={"event_id": "evt-2", "conversation_id": str(conversation.id)},
    )

    assert response.status_code == 202
    assert response.json()["job"] == "process_conversation"
    assert queue.jobs[0]["payload"] == {"conversation_id": str(conversation.id)}


async def test_webhook_is_idempotent_by_header(client: AsyncClient, queue: FakeQueue) -> None:
    """Повтор события с тем же ключом не ставит вторую задачу."""
    headers = {"X-Idempotency-Key": "evt-42"}

    first = await client.post("/webhooks/hh", json={"event": "new_response"}, headers=headers)
    second = await client.post("/webhooks/hh", json={"event": "new_response"}, headers=headers)

    assert first.status_code == 202
    assert first.json()["duplicate"] is False
    assert second.status_code == 202
    assert second.json()["duplicate"] is True
    assert len(queue.jobs) == 1


async def test_webhook_is_idempotent_by_body_hash(client: AsyncClient, queue: FakeQueue) -> None:
    """Без идентификатора события повтор опознаётся по хэшу тела."""
    payload = {"event": "new_response", "vacancy_id": "vac-1"}

    await client.post("/webhooks/hh", json=payload)
    repeated = await client.post("/webhooks/hh", json=payload)

    assert repeated.json()["duplicate"] is True
    assert len(queue.jobs) == 1


async def test_webhook_rejects_bad_signature(store: FakeStore, cache: FakeCache, queue: FakeQueue) -> None:
    """Неверная подпись — 401, задача не ставится."""
    app = _app_with_secret(store=store, cache=cache, queue=queue)
    async with asgi_client(app) as client:
        response = await client.post(
            "/webhooks/hh",
            content=b'{"event_id": "evt-1"}',
            headers={"X-Hirebot-Signature": "sha256=deadbeef", "Content-Type": "application/json"},
        )

    assert response.status_code == 401
    assert queue.jobs == []


async def test_webhook_accepts_valid_signature(store: FakeStore, cache: FakeCache, queue: FakeQueue) -> None:
    """Корректная HMAC-подпись пропускает событие."""
    app = _app_with_secret(store=store, cache=cache, queue=queue)
    body = b'{"event_id": "evt-signed"}'
    async with asgi_client(app) as client:
        response = await client.post(
            "/webhooks/hh",
            content=body,
            headers={
                "X-Hirebot-Signature": f"sha256={_sign(body)}",
                "Content-Type": "application/json",
            },
        )

    assert response.status_code == 202
    assert response.json()["event_id"] == "evt-signed"
    assert len(queue.jobs) == 1


async def test_webhook_accepts_plain_secret_header(
    store: FakeStore, cache: FakeCache, queue: FakeQueue
) -> None:
    """Площадки без HMAC присылают секрет как есть — это тоже поддерживается."""
    app = _app_with_secret(store=store, cache=cache, queue=queue)
    async with asgi_client(app) as client:
        response = await client.post(
            "/webhooks/hh", json={"event_id": "evt-3"}, headers={"X-Webhook-Secret": SECRET}
        )

    assert response.status_code == 202


async def test_webhook_is_503_without_secret_outside_local(store: FakeStore) -> None:
    """Вне local не настроенный секрет — отказ, а не «работаем без проверки»."""
    settings = make_settings(env="staging")
    app = create_app(container=make_container(settings, store=store))

    async with asgi_client(app) as client:
        response = await client.post("/webhooks/hh", json={"event_id": "evt-1"})

    assert response.status_code == 503
    assert "WEBHOOK_SECRET" in response.json()["message"]


async def test_webhook_is_503_for_disabled_channel(client: AsyncClient, queue: FakeQueue) -> None:
    """Уведомление по выключенной площадке обработать нечем."""
    response = await client.post("/webhooks/avito", json={"event_id": "evt-1"})

    assert response.status_code == 503
    assert response.json()["code"] == "channel_not_configured"
    assert queue.jobs == []


async def test_webhook_releases_event_when_queue_is_down(cache: FakeCache) -> None:
    """Если очередь недоступна — 503 и снятая отметка о событии.

    Иначе площадка повторит доставку, а мы отбросим её как дубль и потеряем
    уведомление.
    """
    store = FakeStore()
    queue = FakeQueue(fail=True)
    app = create_app(container=make_container(make_settings(), store=store, cache=cache, queue=queue))

    async with asgi_client(app) as client:
        response = await client.post("/webhooks/hh", json={"event_id": "evt-9"})

    assert response.status_code == 503
    assert "webhook:hh:evt-9" not in cache.counters


async def test_webhook_rejects_unknown_channel(client: AsyncClient) -> None:
    response = await client.post("/webhooks/telegram", json={"event_id": "evt-1"})

    assert response.status_code == 422


async def test_webhook_channel_parameter_is_enum(client: AsyncClient) -> None:
    """Канал приходит перечислением: опечатка не превращается в новый канал."""
    response = await client.post("/webhooks/HH", json={"event_id": "evt-1"})

    assert response.status_code == 422
    assert Channel.HH.value == "hh"


async def test_webhook_dedupes_processing_of_same_conversation(client: AsyncClient, queue: FakeQueue) -> None:
    """Один и тот же диалог дважды не обрабатывается."""
    conversation_id = uuid4()
    headers = {"X-Event-Id": "evt-77"}

    await client.post("/webhooks/hh", json={"conversation_id": str(conversation_id)}, headers=headers)
    repeated = await client.post(
        "/webhooks/hh", json={"conversation_id": str(conversation_id)}, headers=headers
    )

    assert repeated.json()["duplicate"] is True
    assert len(queue.jobs) == 1
