"""Тесты HTML-интерфейса рекрутера.

UI показывает персональные данные кандидатов, поэтому проверяется не только
«страница открывается», но и защита:

* без сессии закрытый UI не отдаёт содержимое, а отправляет на вход;
* ключ проверяется, неверный не пускает;
* POST без CSRF-токена и с чужим ``Origin`` отклоняется — иначе чужая страница
  могла бы отправить приглашение кандидату от имени рекрутера;
* ``next`` не позволяет увести рекрутера на чужой сайт (открытый редирект);
* данные кандидата экранируются (XSS через поле, пришедшее с площадки).

Сеть, БД и Redis не нужны: контейнер собирается на fake-адаптерах, а запросы
идут через ASGI-транспорт в том же процессе.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from hirebot.api.app import create_app
from hirebot.api.ui_security import (
    CSRF_COOKIE,
    SESSION_COOKIE,
    issue_session,
    safe_next,
    verify_session,
)
from hirebot.config import Settings
from hirebot.domain.enums import Channel
from hirebot.services import IngestService
from hirebot.testing.fakes import build_fake_container

SECRET = "ui-test-secret"


def make_settings(*, with_secret: bool = True) -> Settings:
    override: dict[str, Any] = {"env": "test", "log_level": "WARNING", "log_json": False}
    if with_secret:
        override["security"] = {"webhook_secret": SecretStr(SECRET)}
    return Settings(_env_file=None, **override)


async def make_client(*, with_secret: bool = True) -> AsyncIterator[httpx.AsyncClient]:
    container = build_fake_container(make_settings(with_secret=with_secret))
    # Наполняем очередь: три отклика из демо-данных → три диалога.
    await IngestService(container).poll(Channel.HH)
    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(
        transport=transport, base_url="http://test", follow_redirects=False
    ) as client:
        yield client


@pytest.fixture
async def open_client() -> AsyncIterator[httpx.AsyncClient]:
    """Клиент без секрета: локальный доступ открыт."""
    async for client in make_client(with_secret=False):
        yield client


@pytest.fixture
async def locked_client() -> AsyncIterator[httpx.AsyncClient]:
    """Клиент с секретом: без входа доступа нет."""
    async for client in make_client(with_secret=True):
        yield client


async def _login(client: httpx.AsyncClient) -> None:
    """Пройти вход: взять CSRF-куку со страницы и отправить форму."""
    page = await client.get("/ui/login")
    assert page.status_code == 200, page.text
    token = client.cookies.get(CSRF_COOKIE)
    assert token, "CSRF-кука должна выставляться при первом обращении"
    response = await client.post(
        "/ui/login",
        data={"key": SECRET, "csrf_token": token, "next": "/ui/queue"},
    )
    assert response.status_code == 303, response.text
    assert client.cookies.get(SESSION_COOKIE)


# ---------------------------------------------------------------------------
# Доступ
# ---------------------------------------------------------------------------


async def test_queue_renders_dialogs(open_client: httpx.AsyncClient) -> None:
    response = await open_client.get("/ui/queue")

    assert response.status_code == 200
    body = response.text
    # Демо-данные: сильный, слабый и «закрытый» кандидат.
    assert "Алексей Петров" in body
    assert "Очередь диалогов" in body


async def test_locked_ui_redirects_to_login(locked_client: httpx.AsyncClient) -> None:
    response = await locked_client.get("/ui/queue")

    assert response.status_code == 303
    assert response.headers["location"].startswith("/ui/login?next=")


async def test_closed_ui_does_not_leak_candidate_names(locked_client: httpx.AsyncClient) -> None:
    """Без сессии содержимое очереди не отдаётся даже в теле ответа."""
    response = await locked_client.get("/ui/queue")

    assert "Алексей Петров" not in response.text


async def test_login_with_wrong_key_is_rejected(locked_client: httpx.AsyncClient) -> None:
    page = await locked_client.get("/ui/login")
    token = locked_client.cookies.get(CSRF_COOKIE)
    assert page.status_code == 200

    response = await locked_client.post("/ui/login", data={"key": "неверный", "csrf_token": token})

    assert response.status_code == 401
    assert "Неверный ключ" in response.text
    assert not locked_client.cookies.get(SESSION_COOKIE)


async def test_login_with_correct_key_grants_access(locked_client: httpx.AsyncClient) -> None:
    await _login(locked_client)

    response = await locked_client.get("/ui/queue")

    assert response.status_code == 200
    assert "Алексей Петров" in response.text


async def test_logout_removes_session(locked_client: httpx.AsyncClient) -> None:
    await _login(locked_client)
    token = locked_client.cookies.get(CSRF_COOKIE)

    response = await locked_client.post("/ui/logout", data={"csrf_token": token})

    assert response.status_code == 303
    assert not locked_client.cookies.get(SESSION_COOKIE)
    after = await locked_client.get("/ui/queue")
    assert after.status_code == 303


# ---------------------------------------------------------------------------
# CSRF и открытый редирект
# ---------------------------------------------------------------------------


async def test_post_without_csrf_is_rejected(locked_client: httpx.AsyncClient) -> None:
    await _login(locked_client)

    response = await locked_client.post("/ui/poll", data={"channel": "hh"})

    assert response.status_code == 403
    assert "CSRF" in response.text


async def test_post_with_foreign_origin_is_rejected(locked_client: httpx.AsyncClient) -> None:
    """Форма с чужого сайта не должна отправляться от имени рекрутера."""
    await _login(locked_client)
    token = locked_client.cookies.get(CSRF_COOKIE)

    response = await locked_client.post(
        "/ui/poll",
        data={"channel": "hh", "csrf_token": token},
        headers={"Origin": "https://evil.example.com"},
    )

    assert response.status_code == 403


async def test_safe_next_blocks_open_redirect() -> None:
    assert safe_next("//evil.example.com") == "/ui/queue"
    assert safe_next("https://evil.example.com") == "/ui/queue"
    assert safe_next("/etc/passwd") == "/ui/queue"
    assert safe_next("/ui/conversations/abc") == "/ui/conversations/abc"
    assert safe_next(None) == "/ui/queue"


async def test_session_signature_is_verified() -> None:
    token = issue_session(SECRET)

    assert verify_session(SECRET, token)
    assert not verify_session("другой-секрет", token)
    assert not verify_session(SECRET, "123.deadbeef")
    assert not verify_session(SECRET, None)

    expired = issue_session(SECRET, now=datetime.now(tz=UTC) - timedelta(days=2))
    assert not verify_session(SECRET, expired)


# ---------------------------------------------------------------------------
# Карточка диалога
# ---------------------------------------------------------------------------


async def _first_conversation_id(client: httpx.AsyncClient) -> str:
    from hirebot.domain.enums import ConversationState

    container = client._transport.app.state.container  # type: ignore[attr-defined]
    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=10)
    return str(conversations[0].id)


async def test_conversation_card_shows_screening(open_client: httpx.AsyncClient) -> None:
    conversation_id = await _first_conversation_id(open_client)

    response = await open_client.get(f"/ui/conversations/{conversation_id}")

    assert response.status_code == 200
    body = response.text
    assert "Скрининг" in body
    assert "Переписка" in body
    # Есть кнопки действий — интерфейс не только для чтения.
    assert "Прогнать шаг воронки" in body


async def test_unknown_conversation_returns_404(open_client: httpx.AsyncClient) -> None:
    response = await open_client.get("/ui/conversations/00000000-0000-0000-0000-000000000000")

    assert response.status_code == 404
    assert "Диалог не найден" in response.text


async def test_candidate_data_is_escaped(open_client: httpx.AsyncClient) -> None:
    """Имя из площадки — недоверенный ввод: оно не должно стать разметкой."""
    container = open_client._transport.app.state.container  # type: ignore[attr-defined]
    from uuid import uuid4

    from hirebot.domain.entities import Candidate
    from hirebot.services.base import DEFAULT_COMPANY_ID

    async with container.uow_factory() as uow:
        conversation = (
            await uow.conversations.list_by_state(
                list(__import__("hirebot.domain.enums", fromlist=["ConversationState"]).ConversationState),
                limit=1,
            )
        )[0]
        candidate = await uow.candidates.get(conversation.candidate_id)
        payload = Candidate(
            id=uuid4(),
            channel=candidate.channel if candidate else Channel.HH,
            external_id=f"xss-{uuid4().hex[:8]}",
            full_name='<script>alert("xss")</script>',
            company_id=candidate.company_id if candidate else DEFAULT_COMPANY_ID,
        )
        await uow.candidates.upsert(payload)
        conversation.candidate_id = payload.id
        await uow.conversations.upsert(conversation)
        await uow.commit()
        conversation_id = conversation.id

    response = await open_client.get(f"/ui/conversations/{conversation_id}")

    assert response.status_code == 200
    assert "<script>alert" not in response.text, "имя кандидата не должно попадать в разметку как тег"
    assert "&lt;script&gt;" in response.text or "script" in response.text.lower()
