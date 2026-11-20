"""Обработка ошибок: коды, единый формат и отсутствие персональных данных.

Главная проверка здесь — «наружу не утекает ничего лишнего»: ни текста
исключения, ни контактов кандидата, ни трассировки. Клиент получает ``error_id``,
по которому инцидент находится в логе.
"""

from __future__ import annotations

from uuid import uuid4

from api_support import FakeServices, FakeStore, seed_conversation
from httpx import AsyncClient

from hirebot.domain.errors import (
    AuthExpired,
    ChannelUnavailable,
    NotFound,
    PolicyViolation,
    RateLimited,
    SlotConflict,
)

PII_NAME = "Иван Иванов"
PII_EMAIL = "ivan@example.com"
PII_PHONE = "+7 999 123-45-67"


async def test_policy_violation_becomes_409(client: AsyncClient, services: FakeServices) -> None:
    """Доменная ошибка политики — 409, а не 500."""
    services.conversation.error = PolicyViolation(
        f"Кандидат {PII_NAME} ({PII_EMAIL}, {PII_PHONE}) исчерпал лимит автосообщений"
    )

    response = await client.post(f"/api/v1/conversations/{uuid4()}/process")

    assert response.status_code == 409
    body = response.json()
    assert body["code"] == "policy_violation"
    assert body["error_id"]
    assert response.headers["X-Error-ID"] == body["error_id"]


async def test_error_body_contains_no_personal_data(client: AsyncClient, services: FakeServices) -> None:
    """В теле ошибки нет ни текста исключения, ни персональных данных."""
    services.conversation.error = PolicyViolation(
        f"Кандидат {PII_NAME} ({PII_EMAIL}, {PII_PHONE}) исчерпал лимит автосообщений"
    )

    response = await client.post(f"/api/v1/conversations/{uuid4()}/process")

    for secret in (PII_NAME, PII_EMAIL, PII_PHONE, "лимит автосообщений"):
        assert secret not in response.text
    assert response.json()["message"] == "Действие запрещено политикой безопасности"


async def test_not_found_becomes_404(client: AsyncClient, services: FakeServices) -> None:
    services.conversation.error = NotFound("диалог не найден")

    response = await client.post(f"/api/v1/conversations/{uuid4()}/process")

    assert response.status_code == 404
    assert response.json()["code"] == "not_found"


async def test_slot_conflict_becomes_409(client: AsyncClient, services: FakeServices) -> None:
    services.scheduling.error = SlotConflict("слот занят")

    response = await client.post(f"/api/v1/conversations/{uuid4()}/book", json={"slot_index": 0})

    assert response.status_code == 409
    assert response.json()["code"] == "slot_conflict"


async def test_auth_expired_becomes_502(client: AsyncClient, services: FakeServices) -> None:
    """Истёкший токен площадки — ошибка вышестоящей системы (502)."""
    services.conversation.error = AuthExpired("токен отозван")

    response = await client.post(f"/api/v1/conversations/{uuid4()}/process")

    assert response.status_code == 502
    assert response.json()["code"] == "auth_expired"


async def test_rate_limited_returns_retry_after(client: AsyncClient, services: FakeServices) -> None:
    """429 обязан сообщать, когда можно вернуться."""
    services.ingest.error = RateLimited("слишком часто", retry_after_s=42.0)

    response = await client.post("/api/v1/channels/hh/poll")

    assert response.status_code == 429
    body = response.json()
    assert body["code"] == "rate_limited"
    assert body["retry_after_s"] == 42.0
    assert response.headers["Retry-After"] == "42"


async def test_channel_unavailable_becomes_503(client: AsyncClient, services: FakeServices) -> None:
    services.ingest.error = ChannelUnavailable("площадка недоступна")

    response = await client.post("/api/v1/channels/hh/poll")

    assert response.status_code == 503
    assert response.json()["code"] == "channel_unavailable"


async def test_validation_error_does_not_echo_input(client: AsyncClient, store: FakeStore) -> None:
    """422 не возвращает введённые значения: в них могут быть ПДн."""
    conversation = seed_conversation(store, full_name=PII_NAME)

    response = await client.post(
        f"/api/v1/conversations/{conversation.id}/messages",
        json={"text": "ок", "requested_by": "x" * 500},
    )

    assert response.status_code == 422
    body = response.json()
    assert body["code"] == "validation_error"
    assert "x" * 50 not in response.text
    assert PII_NAME not in response.text


async def test_unhandled_error_is_generic_500(raw_client: AsyncClient, services: FakeServices) -> None:
    """Неожиданное исключение наружу не отдаётся: только error_id и общее сообщение."""
    services.conversation.error = RuntimeError(f"внутренняя поломка с {PII_EMAIL}")

    response = await raw_client.post(f"/api/v1/conversations/{uuid4()}/process")

    assert response.status_code == 500
    body = response.json()
    assert body["code"] == "internal_error"
    assert body["error_id"]
    assert PII_EMAIL not in response.text
    assert "внутренняя поломка" not in response.text
