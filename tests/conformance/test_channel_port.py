"""Контракт ``ChannelPort``: набор, который обязан пройти любой адаптер площадки.

Смысл набора — проверять **контракт**, а не детали реализации: те же тесты
должны пройти на hh.ru, Avito и любой следующей площадке. Поэтому ассерты идут
только по методам порта (``send_message``/``list_messages``/…), а данные
готовятся через подготовку двойника.

Что доказывает набор (ARCHITECTURE.md, п. 2.2, 2.4, 3.1):

* поллинг не отдаёт один и тот же отклик дважды и не теряет новые;
* повторное исходящее действие с тем же ``idempotency_key`` не создаёт второе
  сообщение и возвращает согласованный результат;
* закрытое резюме — это ровно ``ResumeAccessDenied`` (штатная деградация,
  а не падение цикла обработки);
* действие выполняется по ключу, а неизвестное действие — доменная ошибка,
  а не молчаливое «ок»;
* ``get_conversation`` сообщает доступность переписки и не расходится с тем,
  разрешает ли адаптер отправку;
* адаптер объявляет свой канал и все методы порта асинхронны.
"""

from __future__ import annotations

import inspect
from datetime import timedelta

import pytest
from conformance_support import (
    NOW,
    ChannelHarness,
    make_message,
    make_response,
    outbound_texts,
)

from hirebot.domain.enums import MessageDirection
from hirebot.domain.errors import DomainError, PolicyViolation, ResumeAccessDenied
from hirebot.ports.channels import ChannelPort

#: Все методы ``ChannelPort``: каждый обязан быть корутиной.
CHANNEL_METHODS: tuple[str, ...] = (
    "healthcheck",
    "list_vacancies",
    "fetch_new_responses",
    "fetch_resume",
    "get_conversation",
    "list_messages",
    "send_message",
    "perform_action",
)


def test_adapter_satisfies_channel_port_protocol(channel_harness: ChannelHarness) -> None:
    """Адаптер обязан структурно соответствовать ``ChannelPort``."""
    assert isinstance(channel_harness.port, ChannelPort)


def test_adapter_declares_its_channel(channel_harness: ChannelHarness) -> None:
    """``channel`` — часть контракта: по нему сервисы выбирают адаптер."""
    assert channel_harness.port.channel is channel_harness.channel


@pytest.mark.parametrize("method_name", CHANNEL_METHODS)
def test_channel_methods_are_coroutines(channel_harness: ChannelHarness, method_name: str) -> None:
    """Каждый метод порта асинхронен: синхронный вызов заблокировал бы воркер."""
    method = getattr(channel_harness.port, method_name)
    assert inspect.iscoroutinefunction(method)


async def test_healthcheck_returns_bool(channel_harness: ChannelHarness) -> None:
    """``healthcheck`` отвечает булевым значением, а не бросает исключение."""
    assert isinstance(await channel_harness.port.healthcheck(), bool)


async def test_list_vacancies_includes_configured_vacancy(channel_harness: ChannelHarness) -> None:
    """Активные вакансии работодателя доступны через порт."""
    vacancies = await channel_harness.port.list_vacancies()
    assert channel_harness.vacancy_external_id in [vacancy.external_id for vacancy in vacancies]


async def test_polling_returns_each_response_exactly_once(channel_harness: ChannelHarness) -> None:
    """Повторный поллинг не повторяет уже выданные отклики (п. 3.1 ARCHITECTURE.md)."""
    first = await channel_harness.port.fetch_new_responses()
    assert [item.conversation_external_id for item in first] == [channel_harness.conversation_external_id]

    repeated = await channel_harness.port.fetch_new_responses()

    assert list(repeated) == [], "повторный поллинг вернул уже обработанный отклик"


async def test_polling_sees_response_that_arrived_later(channel_harness: ChannelHarness) -> None:
    """«Только новые» не означает «пусто всегда»: свежий отклик обязан вернуться."""
    await channel_harness.port.fetch_new_responses()
    later_conversation_id = f"{channel_harness.channel.value}-conformance-chat-2"
    channel_harness.publish_response(
        make_response(
            channel=channel_harness.channel,
            conversation_external_id=later_conversation_id,
            candidate_external_id="conformance-candidate-2",
            resume_external_id=channel_harness.resume_external_id,
            received_at=NOW + timedelta(minutes=10),
        )
    )

    fresh = await channel_harness.port.fetch_new_responses()

    assert [item.conversation_external_id for item in fresh] == [later_conversation_id]


async def test_send_message_is_idempotent_by_key(channel_harness: ChannelHarness) -> None:
    """Повтор отправки с тем же ``idempotency_key`` не создаёт второе сообщение."""
    port = channel_harness.port
    conversation_id = channel_harness.conversation_external_id

    first = await port.send_message(conversation_id, "Здравствуйте!", idempotency_key="msg-1")
    repeated = await port.send_message(conversation_id, "Здравствуйте!", idempotency_key="msg-1")

    assert repeated.external_id == first.external_id, "повтор не вернул тот же результат"
    assert await outbound_texts(port, conversation_id) == ["Здравствуйте!"]


async def test_send_message_with_new_key_delivers_new_message(channel_harness: ChannelHarness) -> None:
    """Дедупликация привязана к ключу, а не «глушит» все следующие отправки."""
    port = channel_harness.port
    conversation_id = channel_harness.conversation_external_id

    await port.send_message(conversation_id, "Первое сообщение", idempotency_key="msg-2a")
    await port.send_message(conversation_id, "Второе сообщение", idempotency_key="msg-2b")

    assert await outbound_texts(port, conversation_id) == ["Первое сообщение", "Второе сообщение"]


async def test_list_messages_filters_by_since(channel_harness: ChannelHarness) -> None:
    """``since`` ограничивает историю переписки снизу — на этом держится разбор ответов."""
    channel_harness.publish_inbound(make_message(external_id="in-1", text="Старое сообщение", created_at=NOW))
    channel_harness.publish_inbound(
        make_message(
            external_id="in-2",
            text="Свежее сообщение",
            created_at=NOW + timedelta(minutes=30),
        )
    )

    messages = await channel_harness.port.list_messages(
        channel_harness.conversation_external_id, since=NOW + timedelta(minutes=15)
    )

    fresh = [message for message in messages if message.direction is MessageDirection.INBOUND]
    assert [message.external_id for message in fresh] == ["in-2"]


async def test_fetch_resume_returns_resume_text(channel_harness: ChannelHarness) -> None:
    """Доступное резюме возвращается с непустым текстом (источник для скрининга)."""
    payload = await channel_harness.port.fetch_resume(channel_harness.resume_external_id)

    assert payload.external_id == channel_harness.resume_external_id
    assert payload.text.strip()


async def test_fetch_resume_of_closed_resume_raises_resume_access_denied(
    channel_harness: ChannelHarness,
) -> None:
    """Закрытое резюме — именно ``ResumeAccessDenied``: это штатная деградация."""
    with pytest.raises(ResumeAccessDenied):
        await channel_harness.port.fetch_resume(channel_harness.closed_resume_external_id)


async def test_fetch_resume_of_unknown_resume_raises_resume_access_denied(
    channel_harness: ChannelHarness,
) -> None:
    """Несуществующее резюме не должно валить цикл общей ошибкой транспорта."""
    with pytest.raises(ResumeAccessDenied):
        await channel_harness.port.fetch_resume("conformance-resume-does-not-exist")


async def test_perform_action_executes_known_action(channel_harness: ChannelHarness) -> None:
    """Действие по ключу действительно выполняется."""
    await channel_harness.port.perform_action(
        channel_harness.conversation_external_id,
        "invitation",
        message="Приглашаем на собеседование",
        idempotency_key="action-1",
    )

    assert channel_harness.performed_actions() == ["invitation"]


async def test_perform_action_rejects_unknown_action(channel_harness: ChannelHarness) -> None:
    """Неизвестное действие — доменная ошибка, а не молчаливое «ок»."""
    with pytest.raises(DomainError):
        await channel_harness.port.perform_action(
            channel_harness.conversation_external_id,
            "no-such-action",
            idempotency_key="action-2",
        )


async def test_get_conversation_messaging_flag_matches_send_behaviour(
    channel_harness: ChannelHarness,
) -> None:
    """``messaging_available`` не расходится с фактической возможностью отправки.

    Если площадка сообщает, что переписка недоступна, отправка обязана упасть
    доменной ошибкой политики (``PolicyViolation``), а не «успешно» отправиться.
    У двойника переписка всегда доступна, поэтому ветка разбирается только для
    адаптеров, умеющих сообщать обратное (hh.ru: ``messaging_status``).
    """
    port = channel_harness.port
    conversation_id = channel_harness.conversation_external_id

    snapshot = await port.get_conversation(conversation_id)

    assert snapshot.external_id == conversation_id
    assert isinstance(snapshot.messaging_available, bool)
    if snapshot.messaging_available:
        sent = await port.send_message(conversation_id, "Проверка доступности", idempotency_key="probe-1")
        assert sent.sent_at is not None
    else:
        with pytest.raises(PolicyViolation):
            await port.send_message(conversation_id, "Проверка доступности", idempotency_key="probe-1")
