"""Тесты адаптера hh.ru (HTTP перехватывается ``respx``)."""

from __future__ import annotations

from datetime import UTC, datetime
from urllib.parse import parse_qs

import httpx
import pytest
from adapter_fakes import ExplodingHttpClient, hh_settings

from hirebot.domain.errors import (
    AuthExpired,
    ChannelNotConfigured,
    ChannelUnavailable,
    PolicyViolation,
    RateLimited,
    ResumeAccessDenied,
)
from hirebot.ports.dto import SentMessage

#: Отклик hh.ru. URL действий намеренно «нестандартные»: адаптер обязан брать их
#: из ответа сервера, а не собирать самостоятельно.
NEGOTIATION_ITEM = {
    "id": "12345",
    "created_at": "2026-11-20T09:30:00+0300",
    "updated_at": "2026-11-20T09:30:00+0300",
    "has_updates": True,
    "url": "https://api.hh.ru/negotiations/response/12345",
    "messages_url": "https://api.hh.ru/negotiations/12345/messages",
    "state": {"id": "response", "name": "Отклик"},
    "employer_state": {"id": "response", "name": "Новый"},
    "counters": {"messages": 1, "unread_messages": 1},
    "source": "NEGOTIATION",
    "resume": {
        "id": "abc123",
        "title": "Python-разработчик",
        "url": "https://api.hh.ru/resumes/abc123?topic_id=12345",
        "first_name": "Иван",
        "last_name": "Петров",
        "total_experience": {"months": 84},
    },
    "actions": [
        {
            "id": "invitation",
            "name": "Пригласить",
            "enabled": True,
            "method": "PUT",
            "url": "https://api.hh.ru/negotiations/custom-collection/12345",
            "arguments": [{"id": "message", "required": True}],
            "resulting_employer_state": {"id": "invitation", "name": "Приглашение"},
        },
        {
            "id": "discard",
            "name": "Отказать",
            "enabled": False,
            "method": "PUT",
            "url": "https://api.hh.ru/negotiations/custom-collection/12345?action=discard",
            "arguments": [{"id": "message", "required": False}],
        },
    ],
}

RESUME_PAYLOAD = {
    "id": "abc123",
    "title": "Python-разработчик",
    "area": {"name": "Москва"},
    "total_experience": {"months": 84},
    "skill_set": ["Python", "PostgreSQL", "Docker"],
    "skills": "Пишу сервисы на Python, люблю автоматизацию.",
    "contact": [{"type": "cell", "value": "+7 999 123-45-67"}],
    "education": {"level": {"name": "Высшее"}, "primary": [{"name": "МГТУ", "year": 2015}]},
    "experience": [
        {
            "position": "Backend-разработчик",
            "company": "ООО Ромашка",
            "start": "2019-01-01",
            "end": "2023-06-01",
            "description": "FastAPI, PostgreSQL, Docker",
        }
    ],
    "hidden_fields": [],
}


async def test_fetch_new_responses_maps_items(respx_mock, hh, cache) -> None:
    adapter = hh()
    collections = respx_mock.get("https://api.hh.ru/negotiations").mock(
        return_value=httpx.Response(
            200,
            json={
                "collections": [
                    {
                        "id": "response",
                        "name": "Все неразобранные",
                        "url": "https://api.hh.ru/negotiations/response?vacancy_id=777",
                        "counters": {"with_updates": 2, "total": 5},
                    }
                ]
            },
        )
    )
    items_route = respx_mock.get("https://api.hh.ru/negotiations/response").mock(
        return_value=httpx.Response(200, json={"found": 1, "items": [NEGOTIATION_ITEM]})
    )

    responses = await adapter.fetch_new_responses(vacancy_external_id="777")

    assert collections.called
    assert items_route.called
    assert len(responses) == 1
    item = responses[0]
    assert item.conversation_external_id == "12345"
    assert item.candidate_external_id == "abc123"
    assert item.vacancy_external_id == "777"
    assert item.candidate_name == "Иван Петров"
    assert item.resume_external_id == "abc123"
    assert item.resume_url == "https://api.hh.ru/resumes/abc123?topic_id=12345"
    assert item.received_at == datetime(2026, 11, 20, 6, 30, tzinfo=UTC)
    assert item.is_new is True

    request = items_route.calls[0].request
    assert request.url.params["show_only_new_responses"] == "true"
    assert request.url.params["per_page"] == "50"
    assert request.url.params["vacancy_id"] == "777"
    assert request.headers["HH-User-Agent"] == "hirebot-test/0.1 (qa@example.com)"
    assert request.headers["Authorization"] == "Bearer test-access-token"
    # URL резюме из ответа запоминается: fetch_resume обязан ходить именно по нему.
    assert await cache.get_json("hh:resume_url:abc123") == NEGOTIATION_ITEM["resume"]["url"]
    assert await cache.get_json("hh:messages_url:12345") == NEGOTIATION_ITEM["messages_url"]


async def test_fetch_new_responses_skips_collection_without_updates(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/negotiations").mock(
        return_value=httpx.Response(
            200,
            json={
                "collections": [
                    {
                        "id": "response",
                        "url": "https://api.hh.ru/negotiations/response?vacancy_id=777",
                        "counters": {"with_updates": 0, "total": 0},
                    }
                ]
            },
        )
    )
    items_route = respx_mock.get("https://api.hh.ru/negotiations/response").mock(
        return_value=httpx.Response(200, json={"items": [NEGOTIATION_ITEM]})
    )

    responses = await adapter.fetch_new_responses(vacancy_external_id="777")

    assert responses == []
    assert not items_route.called, "при with_updates=0 коллекцию запрашивать не нужно"


async def test_perform_action_uses_url_from_actions(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/negotiations/12345").mock(
        return_value=httpx.Response(200, json={**NEGOTIATION_ITEM, "messaging_status": "ok"})
    )
    action_route = respx_mock.put("https://api.hh.ru/negotiations/custom-collection/12345").mock(
        return_value=httpx.Response(204)
    )

    await adapter.perform_action(
        "12345", "invitation", message="Здравствуйте! Приглашаем на собеседование.", idempotency_key="k-1"
    )

    assert action_route.called, "действие выполняется по URL из actions[], а не по угаданному пути"
    form = parse_qs(action_route.calls[0].request.content.decode())
    assert form["message"] == ["Здравствуйте! Приглашаем на собеседование."]


async def test_perform_action_requires_message_for_invitation(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/negotiations/12345").mock(
        return_value=httpx.Response(200, json={**NEGOTIATION_ITEM, "messaging_status": "ok"})
    )

    with pytest.raises(ValueError, match="message"):
        await adapter.perform_action("12345", "invitation", idempotency_key="k-1")


async def test_perform_action_rejects_disabled_action(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/negotiations/12345").mock(
        return_value=httpx.Response(200, json={**NEGOTIATION_ITEM, "messaging_status": "ok"})
    )

    with pytest.raises(PolicyViolation):
        await adapter.perform_action("12345", "discard", message="Отказ", idempotency_key="k-2")


async def test_perform_action_rejects_foreign_host(respx_mock, hh) -> None:
    adapter = hh()
    item = {
        **NEGOTIATION_ITEM,
        "messaging_status": "ok",
        "actions": [
            {
                "id": "invitation",
                "name": "Пригласить",
                "enabled": True,
                "method": "PUT",
                "url": "https://evil.example.com/collect",
                "arguments": [{"id": "message", "required": True}],
            }
        ],
    }
    respx_mock.get("https://api.hh.ru/negotiations/12345").mock(return_value=httpx.Response(200, json=item))

    with pytest.raises(ChannelUnavailable, match="чужой хост"):
        await adapter.perform_action("12345", "invitation", message="текст", idempotency_key="k-3")


async def test_get_conversation_parses_actions_and_messaging_status(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/negotiations/12345").mock(
        return_value=httpx.Response(200, json={**NEGOTIATION_ITEM, "messaging_status": "ok"})
    )

    snapshot = await adapter.get_conversation("12345")

    assert snapshot.external_id == "12345"
    assert snapshot.messaging_available is True
    assert snapshot.unread_count == 1
    invitation = next(action for action in snapshot.actions if action.key == "invitation")
    assert invitation.requires_message is True
    assert invitation.method == "PUT"
    assert invitation.url == "https://api.hh.ru/negotiations/custom-collection/12345"
    assert invitation.extra_args["arguments"] == {"message": True}


@pytest.mark.parametrize(
    ("status", "body", "expected"),
    [
        (403, {"type": "resumes", "value": "quota_exceeded"}, ResumeAccessDenied),
        (403, {"type": "resumes", "value": "cant_view_contacts"}, ResumeAccessDenied),
        (404, {"type": "negotiations", "value": "resume_not_found"}, ResumeAccessDenied),
        (429, {"type": "resumes", "value": "view_limit_exceeded"}, RateLimited),
        (401, {"type": "oauth", "value": "token_expired"}, AuthExpired),
    ],
)
async def test_fetch_resume_maps_errors(respx_mock, hh, status, body, expected) -> None:
    """Каждая ошибка площадки превращается в конкретную доменную ошибку.

    Для 401 ожидается ``AuthExpired``, а не общий ``ChannelUnavailable``: смысл
    разный — «нужна переавторизация» против «временная недоступность, повторить».
    Ретраить по 401 бессмысленно, и вызывающая сторона должна это различать.
    """
    adapter = hh()
    respx_mock.get("https://api.hh.ru/resumes/xyz").mock(
        return_value=httpx.Response(status, json=body, headers={"Retry-After": "60"})
    )

    with pytest.raises(expected):
        await adapter.fetch_resume("xyz")


async def test_rate_limited_carries_retry_after(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/resumes/xyz").mock(
        return_value=httpx.Response(
            429, json={"type": "resumes", "value": "view_limit_exceeded"}, headers={"Retry-After": "60"}
        )
    )

    with pytest.raises(RateLimited) as exc_info:
        await adapter.fetch_resume("xyz")

    assert exc_info.value.retry_after_s == 60.0


async def test_fetch_resume_uses_registered_url_and_normalizes_text(respx_mock, hh, cache) -> None:
    adapter = hh()
    await cache.set_json("hh:resume_url:abc123", "https://api.hh.ru/resumes/abc123?topic_id=12345")
    route = respx_mock.get("https://api.hh.ru/resumes/abc123").mock(
        return_value=httpx.Response(200, json=RESUME_PAYLOAD)
    )

    payload = await adapter.fetch_resume("abc123")

    assert route.called, "резюме запрашивается по URL из ответа площадки"
    assert route.calls[0].request.url.params["topic_id"] == "12345"
    assert "Python-разработчик" in payload.text
    assert "Москва" in payload.text
    assert "Backend-разработчик" in payload.text
    assert "ООО Ромашка" in payload.text
    assert "Python, PostgreSQL, Docker" in payload.text
    assert payload.parsed["total_experience_months"] == 84
    assert payload.contacts["cell"] == "+7 999 123-45-67"
    # ФИО не должно попадать в текст, который уходит в LLM.
    assert "Иван" not in payload.text
    assert "Петров" not in payload.text


async def test_healthcheck_false_without_token(hh) -> None:
    adapter = hh(settings=hh_settings(access_token=None))

    assert await adapter.healthcheck() is False


async def test_send_message_checks_messaging_status(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/negotiations/12345").mock(
        return_value=httpx.Response(200, json={**NEGOTIATION_ITEM, "messaging_status": "no_invitation"})
    )
    post = respx_mock.post("https://api.hh.ru/negotiations/12345/messages").mock(
        return_value=httpx.Response(201)
    )

    with pytest.raises(PolicyViolation, match="приглашения"):
        await adapter.send_message("12345", "текст", idempotency_key="k-4")

    assert not post.called, "без приглашения сообщение не отправляется"


async def test_send_message_returns_external_id_from_history(respx_mock, hh, clock) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/negotiations/12345").mock(
        return_value=httpx.Response(200, json={**NEGOTIATION_ITEM, "messaging_status": "ok"})
    )
    post = respx_mock.post("https://api.hh.ru/negotiations/12345/messages").mock(
        return_value=httpx.Response(201)
    )
    respx_mock.get("https://api.hh.ru/negotiations/12345/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "m-1",
                        "text": "  Спасибо за отклик!  ",
                        "created_at": "2026-11-20T10:05:00+0300",
                        "author": {"participant_type": "employer"},
                    }
                ]
            },
        )
    )

    sent = await adapter.send_message(
        "12345", "Спасибо за отклик!", idempotency_key="k-5", kind_hint="greeting"
    )

    assert post.called
    form = parse_qs(post.calls[0].request.content.decode())
    assert form["message"] == ["Спасибо за отклик!"]
    assert isinstance(sent, SentMessage)
    assert sent.external_id == "m-1"
    assert sent.sent_at == clock.now()


async def test_list_messages_maps_direction_and_filters_since(respx_mock, hh) -> None:
    adapter = hh()
    await cache_register_messages(adapter)
    respx_mock.get("https://api.hh.ru/negotiations/12345/messages").mock(
        return_value=httpx.Response(
            200,
            json={
                "items": [
                    {
                        "id": "m-1",
                        "text": "Здравствуйте, я заинтересован",
                        "created_at": "2026-11-20T10:00:00+0300",
                        "author": {"participant_type": "applicant", "name": "Иван"},
                    },
                    {
                        "id": "m-2",
                        "text": "Спасибо, уточним",
                        "created_at": "2026-11-19T10:00:00+0300",
                        "author": {"participant_type": "employer"},
                    },
                ]
            },
        )
    )

    messages = await adapter.list_messages("12345", since=datetime(2026, 11, 20, tzinfo=UTC))

    assert len(messages) == 1
    message = messages[0]
    assert message.external_id == "m-1"
    assert message.direction.value == "inbound"
    assert message.created_at == datetime(2026, 11, 20, 7, 0, tzinfo=UTC)


async def cache_register_messages(adapter) -> None:
    """Зарегистрировать URL переписки так, как это делает чтение откликов."""
    await adapter._cache.set_json("hh:messages_url:12345", "https://api.hh.ru/negotiations/12345/messages")


async def test_list_vacancies_reads_me_and_caches_result(respx_mock, hh) -> None:
    adapter = hh()
    me = respx_mock.get("https://api.hh.ru/me").mock(
        return_value=httpx.Response(200, json={"employer": {"id": "999"}, "manager": {"id": "1"}})
    )
    vacancies = respx_mock.get("https://api.hh.ru/employers/999/vacancies/active").mock(
        return_value=httpx.Response(
            200,
            json={"items": [{"id": "v1", "name": "Python-разработчик", "area": {"name": "Москва"}}]},
        )
    )

    first = await adapter.list_vacancies()
    second = await adapter.list_vacancies()

    assert [vacancy.external_id for vacancy in first] == ["v1"]
    assert first[0].title == "Python-разработчик"
    assert [vacancy.external_id for vacancy in second] == ["v1"]
    assert me.call_count == 1
    assert vacancies.call_count == 1, "второй вызов обслуживается кэшем (TTL 5 минут)"


async def test_healthcheck_true_when_me_available(respx_mock, hh) -> None:
    adapter = hh()
    respx_mock.get("https://api.hh.ru/me").mock(
        return_value=httpx.Response(200, json={"employer": {"id": "999"}})
    )

    assert await adapter.healthcheck() is True


async def test_disabled_channel_does_not_touch_network() -> None:
    from adapter_fakes import make_hh_adapter

    exploding = ExplodingHttpClient()
    adapter = make_hh_adapter(settings=hh_settings(enabled=False), http=exploding)

    assert await adapter.healthcheck() is False
    with pytest.raises(ChannelNotConfigured):
        await adapter.list_vacancies()
    with pytest.raises(ChannelNotConfigured):
        await adapter.fetch_new_responses()
    assert exploding.calls == []


async def test_refresh_on_token_expired_retries_once(respx_mock, hh, clock, cache) -> None:
    """403 ``token_expired`` -> принудительный refresh -> повтор запроса."""
    from adapter_fakes import MemoryCache, make_hh_adapter

    store = MemoryCache(clock=clock)
    # Токен в кэше ещё ЖИВОЙ: тогда единственный refresh в тесте — вынужденный,
    # после ответа ``403 token_expired``. Если сделать его протухшим, адаптер
    # сначала обновится проактивно, и проверка «ровно один refresh» потеряет смысл.
    await store.set_json(
        "hh:tokens:test-client",
        {
            "access_token": "stale",
            "refresh_token": "refresh-1",
            "expires_at": datetime(2026, 11, 21, 7, 0, tzinfo=UTC).isoformat(),
        },
    )
    adapter = make_hh_adapter(
        settings=hh_settings(access_token=None, refresh_token=None),
        cache=store,
        clock=clock,
    )
    refresh = respx_mock.post("https://hh.ru/oauth/token").mock(
        return_value=httpx.Response(
            200,
            json={
                "access_token": "fresh",
                "refresh_token": "refresh-2",
                "expires_in": 1_209_600,
                "token_type": "bearer",
            },
        )
    )
    me = respx_mock.get("https://api.hh.ru/me").mock(
        side_effect=[
            httpx.Response(403, json={"type": "oauth", "value": "token_expired"}),
            httpx.Response(200, json={"employer": {"id": "999"}}),
        ]
    )

    result = await adapter.healthcheck()

    assert result is True
    assert refresh.call_count == 1
    assert me.call_count == 2
    assert me.calls[1].request.headers["Authorization"] == "Bearer fresh"
