"""Тесты административных маршрутов.

Проверяется главное для слоя доставки: роутеры не содержат логики и вызывают
сервисы с правильными аргументами, а ответы собираются из схем с замаскированными
контактами кандидата.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from api_support import (
    FakeChannel,
    FakeServices,
    FakeStore,
    asgi_client,
    make_booking,
    make_container,
    make_settings,
    make_slot,
    seed_conversation,
    seed_message,
    seed_screening,
)
from httpx import AsyncClient
from pydantic import SecretStr

from hirebot.api import deps
from hirebot.api.app import create_app
from hirebot.config import SecuritySettings
from hirebot.domain.enums import Channel, ConversationState

# ---------------------------------------------------------------------------
# Поллинг
# ---------------------------------------------------------------------------


async def test_poll_calls_service_and_returns_counters(client: AsyncClient, services: FakeServices) -> None:
    """Ручной поллинг дергает ``IngestService.poll`` и отдаёт счётчики."""
    response = await client.post("/api/v1/channels/hh/poll?limit=10")

    assert response.status_code == 200
    body = response.json()
    assert services.ingest.calls == [(Channel.HH, 10)]
    assert body["channel"] == "hh"
    assert body["counters"] == {"fetched": 3, "created": 2, "skipped": 1, "outcomes_count": 1}
    assert body["correlation_id"]


async def test_poll_rejects_disabled_channel(client: AsyncClient, services: FakeServices) -> None:
    """Выключенная площадка — 503, сервис не вызывается."""
    response = await client.post("/api/v1/channels/avito/poll")

    assert response.status_code == 503
    assert response.json()["code"] == "channel_not_configured"
    assert services.ingest.calls == []


async def test_poll_rejects_unknown_channel(client: AsyncClient) -> None:
    """Несуществующий канал отсекается валидацией пути."""
    response = await client.post("/api/v1/channels/telegram/poll")

    assert response.status_code == 422


async def test_poll_requires_limit_to_be_sane(client: AsyncClient, services: FakeServices) -> None:
    """Лимит больше разрешённого — 422, в сервис ничего не уходит."""
    response = await client.post("/api/v1/channels/hh/poll?limit=1000")

    assert response.status_code == 422
    assert services.ingest.calls == []


async def test_poll_is_503_when_service_layer_is_missing(app, monkeypatch) -> None:
    """Без сервисного слоя эндпоинт честно отвечает 503, а не 500."""

    def _raise(name: str) -> None:
        msg = f"нет модуля {name}"
        raise ImportError(msg)

    monkeypatch.setattr(deps, "import_module", _raise)
    async with asgi_client(app) as client:
        response = await client.post("/api/v1/channels/hh/poll")

    assert response.status_code == 503


# ---------------------------------------------------------------------------
# Список диалогов и карточка
# ---------------------------------------------------------------------------


async def test_list_conversations_with_state_filter(client: AsyncClient, store: FakeStore) -> None:
    """Список фильтруется по состоянию."""
    seed_conversation(store, state=ConversationState.NEW, external_id="c-new")
    seed_conversation(store, state=ConversationState.BOOKED, external_id="c-booked")

    response = await client.get("/api/v1/conversations?state=booked")

    assert response.status_code == 200
    body = response.json()
    assert body["count"] == 1
    assert body["items"][0]["state"] == "booked"
    assert body["items"][0]["external_id"] == "c-booked"


async def test_list_conversations_paginates(client: AsyncClient, store: FakeStore) -> None:
    """Пагинация не отдаёт лишнего и не теряет элементы."""
    for index in range(3):
        seed_conversation(store, external_id=f"c-{index}")

    response = await client.get("/api/v1/conversations?limit=1&offset=1")

    assert response.status_code == 200
    body = response.json()
    assert body["limit"] == 1
    assert body["offset"] == 1
    assert body["count"] == 1


async def test_conversation_card_masks_contacts(client: AsyncClient, store: FakeStore) -> None:
    """Карточка содержит кандидата, вакансию, скрининг и переписку.

    Контакты приходят только замаскированными: полный номер и адрес не покидают
    площадку (152-ФЗ, минимизация данных).
    """
    conversation = seed_conversation(store)
    seed_screening(store, conversation)
    seed_message(store, conversation, body="Здравствуйте, Иван!")

    response = await client.get(f"/api/v1/conversations/{conversation.id}")

    assert response.status_code == 200
    body = response.json()
    assert body["candidate"]["full_name"] == "Иван Иванов"
    assert body["candidate"]["phone_masked"] == "***67"
    assert body["candidate"]["email_masked"] == "i***@example.com"
    assert body["candidate"]["has_phone"] is True
    assert "+7 999 123-45-67" not in response.text
    assert "ivan@example.com" not in response.text
    assert body["vacancy"]["title"] == "Python-разработчик"
    assert body["screening"]["total_score"] == 82.0
    assert [message["body"] for message in body["messages"]] == ["Здравствуйте, Иван!"]
    assert body["booking"] is None


async def test_conversation_card_includes_booking(client: AsyncClient, store: FakeStore) -> None:
    """Если репозиторий умеет искать бронь по диалогу — она попадает в карточку."""
    conversation = seed_conversation(store)
    store.bookings.append(make_booking(conversation))

    response = await client.get(f"/api/v1/conversations/{conversation.id}")

    assert response.status_code == 200
    booking = response.json()["booking"]
    assert booking["external_meeting_id"] == "meeting-1"
    assert booking["status"] == "confirmed"


async def test_conversation_card_returns_404_for_unknown_id(client: AsyncClient) -> None:
    """Неизвестный диалог — 404 с единым форматом ошибки."""
    response = await client.get(f"/api/v1/conversations/{uuid4()}")

    assert response.status_code == 404
    body = response.json()
    assert body["code"] == "not_found"
    assert body["error_id"]
    assert response.headers["X-Error-ID"] == body["error_id"]


async def test_conversation_card_rejects_broken_uuid(client: AsyncClient) -> None:
    response = await client.get("/api/v1/conversations/not-a-uuid")

    assert response.status_code == 422
    assert response.json()["code"] == "validation_error"


# ---------------------------------------------------------------------------
# Шаги воронки
# ---------------------------------------------------------------------------


async def test_process_step_calls_conversation_service(client: AsyncClient, services: FakeServices) -> None:
    """Шаг воронки выполняется сервисом, роутер только передаёт идентификатор."""
    conversation_id = uuid4()

    response = await client.post(f"/api/v1/conversations/{conversation_id}/process")

    assert response.status_code == 200
    body = response.json()
    assert services.conversation.calls == [conversation_id]
    assert body["state"] == "questions_sent"
    assert body["counters"] == {"steps": 3, "sent": 1}


async def test_propose_slots_returns_slots(client: AsyncClient, services: FakeServices) -> None:
    """Слоты возвращаются с длительностью и часовым поясом менеджера."""
    services.scheduling.slots = [make_slot(start_offset_hours=24), make_slot(start_offset_hours=48)]
    conversation_id = uuid4()

    response = await client.post(
        f"/api/v1/conversations/{conversation_id}/slots",
        json={"days_ahead": 3, "limit": 2},
    )

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 2
    assert body[0]["duration_minutes"] == 60
    assert body[0]["timezone"] == "Europe/Moscow"
    assert services.scheduling.slot_calls == [
        {"conversation_id": conversation_id, "days_ahead": 3, "limit": 2}
    ]


async def test_propose_slots_without_body_uses_defaults(client: AsyncClient, services: FakeServices) -> None:
    """Тело запроса необязательно: подставляются значения по умолчанию."""
    conversation_id = uuid4()

    response = await client.post(f"/api/v1/conversations/{conversation_id}/slots")

    assert response.status_code == 200
    assert services.scheduling.slot_calls[0]["days_ahead"] == 5
    assert services.scheduling.slot_calls[0]["limit"] == 3


async def test_book_interview_passes_slot_index(
    client: AsyncClient, services: FakeServices, store: FakeStore
) -> None:
    """Обратная совместимость: бронь по индексу слота из ответа ``/slots``."""
    conversation = seed_conversation(store)
    services.scheduling.booking = make_booking(conversation)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/book",
        json={"slot_index": 1, "requested_by": "recruiter"},
    )

    assert response.status_code == 200
    assert response.json()["external_meeting_id"] == "meeting-1"
    assert services.scheduling.book_calls == [
        {
            "conversation_id": conversation.id,
            "slot_index": 1,
            "slot_start": None,
            "requested_by": "recruiter",
        }
    ]


async def test_book_interview_prefers_explicit_slot_start(
    client: AsyncClient, services: FakeServices, store: FakeStore
) -> None:
    """Явное время надёжнее индекса: индекс указывает на **пересчитанную** сетку.

    Между показом слотов кандидату и подтверждением календарь мог измениться,
    и тот же индекс адресовал бы уже другое время.
    """
    conversation = seed_conversation(store)
    services.scheduling.booking = make_booking(conversation)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/book",
        json={"slot_start": "2026-06-01T10:00:00+00:00", "slot_index": 3},
    )

    assert response.status_code == 200
    call = services.scheduling.book_calls[0]
    assert call["slot_start"] == datetime(2026, 6, 1, 10, 0, tzinfo=UTC)
    assert call["slot_index"] is None, "при явном времени индекс обязан игнорироваться"


# ---------------------------------------------------------------------------
# Ручное сообщение
# ---------------------------------------------------------------------------


async def test_manual_message_goes_to_outbox(client: AsyncClient, store: FakeStore, queue) -> None:
    """Ручное сообщение пишется в БД и outbox одной транзакцией, отправляет воркер."""
    conversation = seed_conversation(store)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"text": "Добрый день! Уточните, пожалуйста, опыт.", "requested_by": "recruiter"},
    )

    assert response.status_code == 202
    body = response.json()
    assert body["status"] == "queued"
    assert body["conversation_id"] == str(conversation.id)
    assert body["requested_by"] == "recruiter"

    assert len(store.outbox) == 1
    entry = store.outbox[0]
    assert entry.topic == "send_message"
    assert entry.payload["message_id"] == body["message_id"]
    assert entry.payload["conversation_id"] == str(conversation.id)
    assert entry.dedupe_key == f"manual-message:{body['message_id']}"
    assert [message.body for message in store.messages] == ["Добрый день! Уточните, пожалуйста, опыт."]
    # Задача диспетчера поставлена, чтобы сообщение ушло не дожидаясь cron.
    assert [job["job"] for job in queue.jobs] == ["dispatch_outbox"]


async def test_manual_message_404_for_unknown_conversation(client: AsyncClient) -> None:
    response = await client.post(f"/api/v1/conversations/{uuid4()}/messages", json={"text": "Привет"})

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_manual_message_rejects_empty_text(client: AsyncClient, store: FakeStore) -> None:
    """Пустое сообщение не проходит валидацию схемы."""
    conversation = seed_conversation(store)

    response = await client.post(f"/api/v1/conversations/{conversation.id}/messages", json={"text": ""})

    assert response.status_code == 422
    assert store.outbox == []


async def test_manual_message_fails_if_channel_is_disabled(store: FakeStore) -> None:
    """Сообщение нельзя отправить в выключенную площадку."""
    conversation = seed_conversation(store, channel=Channel.AVITO, external_id="avito-1")
    container = make_container(make_settings(), store=store, channels={Channel.HH: FakeChannel(Channel.HH)})

    async with asgi_client(create_app(container=container)) as client:
        response = await client.post(
            f"/api/v1/conversations/{conversation.id}/messages", json={"text": "Привет"}
        )

    assert response.status_code == 503
    assert store.outbox == []


# ---------------------------------------------------------------------------
# Аутентификация
# ---------------------------------------------------------------------------


async def test_api_key_is_required_when_secret_is_set(store: FakeStore) -> None:
    """С заданным секретом административные маршруты закрыты для анонима."""
    settings = make_settings(webhook_secret="s3cret")
    app = create_app(container=make_container(settings, store=store))

    async with asgi_client(app) as client:
        anonymous = await client.get("/api/v1/conversations")
        authorized = await client.get("/api/v1/conversations", headers={"X-API-Key": "s3cret"})
        wrong = await client.get("/api/v1/conversations", headers={"X-API-Key": "nope"})
        health = await client.get("/health")

    assert anonymous.status_code == 401
    assert wrong.status_code == 401
    assert authorized.status_code == 200
    # Проверки живости остаются открытыми: их дёргает мониторинг.
    assert health.status_code == 200


async def test_api_is_open_in_local_env_without_secret(client: AsyncClient) -> None:
    """Локально (демо, тесты) доступ открыт — иначе демо требовало бы секретов."""
    response = await client.get("/api/v1/conversations")

    assert response.status_code == 200


async def test_non_local_env_without_secret_is_503(store: FakeStore) -> None:
    """Вне local отсутствие секрета — не «открытый доступ», а небезопасная конфигурация."""
    settings = make_settings(env="staging")
    app = create_app(container=make_container(settings, store=store))

    async with asgi_client(app) as client:
        response = await client.get("/api/v1/conversations")

    assert response.status_code == 503
    assert "WEBHOOK_SECRET" in response.json()["message"]


async def test_settings_object_exposes_secret_as_secretstr() -> None:
    """Секрет хранится в ``SecretStr``: он не попадёт в логи и ``repr``."""
    settings = make_settings(webhook_secret="s3cret")

    assert isinstance(settings.security.webhook_secret, SecretStr)
    assert "s3cret" not in repr(SecuritySettings(webhook_secret=SecretStr("s3cret")))
