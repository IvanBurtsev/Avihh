"""Дымовой тест пакетной обработки: пачка откликов не ломает состояние.

Полноценный замер — в ``scripts/load_test.py`` (сотни откликов на SQLite).
Здесь проверяется то, что должно ломаться первым при росте объёма:

* пачка обрабатывается без ошибок;
* каждый диалог получает терминальное или рабочее состояние, а не «зависает»;
* счётчики не теряют отклики.

Тест намеренно небольшой: он входит в обычный прогон и не должен замедлять его.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

import pytest

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.enums import Channel, ConversationState
from hirebot.ports.dto import InboundResponse
from hirebot.services import ConversationService, IngestService
from hirebot.services.base import DEFAULT_COMPANY_ID
from hirebot.testing.fakes import FakeChannelAdapter

BATCH_SIZE = 30


@pytest.fixture
async def container() -> Any:
    result = build_fake_container(Settings(env="test", log_level="ERROR", _env_file=None))
    await IngestService(result).poll(Channel.HH)
    return result


async def test_batch_of_responses_is_processed_without_errors(container: Any) -> None:
    async with container.uow_factory() as uow:
        vacancies = await uow.vacancies.list_active(DEFAULT_COMPANY_ID)
    assert vacancies
    vacancy = vacancies[0]

    channel = container.channels[Channel.HH]
    assert isinstance(channel, FakeChannelAdapter)
    now = datetime.now(tz=UTC)
    for index in range(BATCH_SIZE):
        channel.add_response(
            InboundResponse(
                channel=Channel.HH,
                conversation_external_id=f"batch-conv-{index}",
                candidate_external_id=f"batch-cand-{index}",
                vacancy_external_id=vacancy.external_id,
                candidate_name=f"Кандидат {index}",
                resume_external_id=None,
                cover_letter="Python, SQL, опыт коммерческой разработки.",
                received_at=now,
            )
        )

    found = 0
    while found < BATCH_SIZE:
        page = await IngestService(container).poll(Channel.HH, limit=50)
        found += page.found
        assert not page.errors, f"ошибки приёма: {page.errors}"
        if page.found == 0:
            break

    async with container.uow_factory() as uow:
        conversations = list(
            await uow.conversations.list_by_state(
                [ConversationState.NEW, ConversationState.RESUME_FETCHED],
                limit=BATCH_SIZE + 20,
            )
        )

    ours = [item for item in conversations if item.external_id.startswith("batch-conv-")]
    assert len(ours) >= BATCH_SIZE, f"создано диалогов пачки: {len(ours)}"

    failures: list[str] = []
    for conversation in ours:
        try:
            await ConversationService(container).process(conversation.id)
        except Exception as exc:
            failures.append(f"{type(exc).__name__}: {exc}")

    assert failures == [], f"пачка обработана со сбоями: {failures[:3]}"

    async with container.uow_factory() as uow:
        stuck = await uow.conversations.list_by_state([ConversationState.NEW], limit=300)
    # Демо-диалоги не входят в пачку: проверяем только те, что мы создали.
    ours = [item for item in stuck if item.external_id.startswith("batch-conv-")]
    assert not ours, "после обработки не должно остаться диалогов пачки в NEW"


async def test_batch_does_not_duplicate_conversations(container: Any) -> None:
    """Повторный опрос не создаёт дублей: идемпотентность важнее скорости."""
    channel = container.channels[Channel.HH]
    assert isinstance(channel, FakeChannelAdapter)
    async with container.uow_factory() as uow:
        vacancies = await uow.vacancies.list_active(DEFAULT_COMPANY_ID)
    vacancy = vacancies[0]
    response = InboundResponse(
        channel=Channel.HH,
        conversation_external_id="dup-conv-1",
        candidate_external_id="dup-cand-1",
        vacancy_external_id=vacancy.external_id,
        candidate_name="Дубль",
        received_at=datetime.now(tz=UTC),
    )
    channel.add_response(response)

    await IngestService(container).poll(Channel.HH)
    await IngestService(container).poll(Channel.HH)

    async with container.uow_factory() as uow:
        conversations = list(await uow.conversations.list_by_state(list(ConversationState), limit=500))
    matching = [
        item for item in conversations if item.external_id == "dup-conv-1" and item.channel is Channel.HH
    ]
    assert len(matching) == 1, "повторный опрос не должен создавать второй диалог"


async def test_batch_state_is_consistent(container: Any) -> None:
    """После пачки не остаётся «полуобработанных» состояний без причины."""
    channel = container.channels[Channel.HH]
    assert isinstance(channel, FakeChannelAdapter)
    async with container.uow_factory() as uow:
        vacancies = await uow.vacancies.list_active(DEFAULT_COMPANY_ID)
    vacancy = vacancies[0]
    now = datetime.now(tz=UTC)
    for index in range(5):
        channel.add_response(
            InboundResponse(
                channel=Channel.HH,
                conversation_external_id=f"state-conv-{index}",
                candidate_external_id=f"state-cand-{index}",
                vacancy_external_id=vacancy.external_id,
                candidate_name=f"Кандидат {index}",
                received_at=now,
            )
        )
    await IngestService(container).poll(Channel.HH)

    async with container.uow_factory() as uow:
        conversations = list(
            await uow.conversations.list_by_state(
                [ConversationState.NEW, ConversationState.RESUME_FETCHED], limit=50
            )
        )
    for conversation in conversations:
        await ConversationService(container).process(conversation.id)

    async with container.uow_factory() as uow:
        final = list(await uow.conversations.list_by_state(list(ConversationState), limit=500))
    for conversation in final:
        assert conversation.state is not None
        if conversation.external_id.startswith("state-conv-"):
            assert conversation.state is not ConversationState.NEW, (
                f"диалог {conversation.external_id} остался в NEW"
            )
