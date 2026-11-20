"""Тесты приёма откликов: поллинг, дедупликация, изоляция ошибок."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from services_support import (
    CONVERSATION_EXTERNAL_ID,
    FIXED_NOW,
    seed_pipeline,
    seed_response,
    seed_vacancy,
    store_of,
)

from hirebot.container import Container
from hirebot.domain.enums import Channel, ConversationState, OutboxStatus
from hirebot.domain.errors import ChannelUnavailable
from hirebot.services.ingest import IngestService
from hirebot.testing.fakes import FakeChannelAdapter, InMemoryJobQueue


async def test_poll_creates_conversation_and_queues_step(container: Container) -> None:
    """Новый отклик превращается в диалог, задачу в outbox и пробуждение воркера."""
    await seed_pipeline(container)

    result = await IngestService(container).poll(Channel.HH)

    assert result.found == 1
    assert result.created == 1
    assert result.duplicates == 0
    assert result.errors == []

    store = await store_of(container)
    assert len(store.conversations) == 1
    conversation = next(iter(store.conversations.values()))
    assert conversation.state is ConversationState.NEW
    assert conversation.channel is Channel.HH
    assert conversation.external_id == CONVERSATION_EXTERNAL_ID

    entries = list(store.outbox.values())
    assert len(entries) == 1
    assert entries[0].topic == "process_conversation"
    assert entries[0].payload == {"conversation_id": str(conversation.id)}
    assert entries[0].status is OutboxStatus.PENDING

    queue = container.queue
    assert isinstance(queue, InMemoryJobQueue)
    assert queue.names() == ["process_conversation"]


async def test_second_poll_returns_nothing_new(container: Container) -> None:
    """Канал отдаёт только новые отклики: повторный поллинг пуст."""
    await seed_pipeline(container)
    service = IngestService(container)

    first = await service.poll(Channel.HH)
    second = await service.poll(Channel.HH)

    assert (first.found, first.created) == (1, 1)
    assert (second.found, second.created, second.duplicates) == (0, 0, 0)


async def test_repeated_ingest_of_processed_response_is_duplicate(container: Container) -> None:
    """Отклик, который уже в работе, не порождает второй диалог и вторую задачу."""
    response = await seed_pipeline(container)
    service = IngestService(container)
    first = await service.ingest(response)
    assert first.created is True
    assert first.conversation_id is not None

    # Диалог ушёл дальше состояния NEW — значит, отклик уже в работе.
    async with container.uow_factory() as uow:
        conversation = uow.store.conversations[first.conversation_id]
        conversation.state = ConversationState.INVITED
        await uow.conversations.upsert(conversation)
        await uow.commit()

    outcome = await service.ingest(response)

    assert outcome.created is False
    assert outcome.reason == "duplicate:invited"
    assert outcome.conversation_id == first.conversation_id

    store = await store_of(container)
    assert len(store.conversations) == 1
    assert len(store.outbox) == 1


async def test_repeated_ingest_before_processing_requeues(container: Container) -> None:
    """Диалог, застрявший в ``NEW``, получает задачу повторно, но не дублируется."""
    response = await seed_pipeline(container)
    service = IngestService(container)

    first = await service.ingest(response)
    second = await service.ingest(response)

    assert first.created is True
    assert second.created is False
    assert second.reason == "already_pending"

    store = await store_of(container)
    assert len(store.conversations) == 1
    # UNIQUE(dedupe_key) не даёт положить одну и ту же задачу дважды.
    assert len(store.outbox) == 1


async def test_poll_isolates_failed_response(container: Container, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ошибка на одном отклике не роняет пачку и попадает в ``errors``."""
    await seed_vacancy(container)
    await seed_response(container, conversation_external_id="hh-chat-1")
    await seed_response(
        container,
        conversation_external_id="hh-chat-2",
        candidate_external_id="hh-cand-2",
        resume_external_id="hh-resume-2",
        received_at=FIXED_NOW + timedelta(minutes=1),
    )

    service = IngestService(container)
    original_ingest = service.ingest

    async def flaky(response: Any) -> Any:
        if response.conversation_external_id == "hh-chat-1":
            msg = "канал недоступен при разборе отклика"
            raise ChannelUnavailable(msg)
        return await original_ingest(response)

    monkeypatch.setattr(service, "ingest", flaky)

    result = await service.poll(Channel.HH, limit=10)

    assert result.found == 2
    assert result.created == 1
    assert len(result.errors) == 1
    assert "hh-chat-1" in result.errors[0]
    store = await store_of(container)
    assert len(store.conversations) == 1


async def test_poll_of_unconfigured_channel_reports_error(container: Container) -> None:
    """Выключенный канал — это не исключение, а ошибка в отчёте."""
    container.channels.pop(Channel.AVITO)

    result = await IngestService(container).poll(Channel.AVITO)

    assert result.found == 0
    assert len(result.errors) == 1
    assert "avito" in result.errors[0]


async def test_ingest_creates_candidate_and_resume_stub(container: Container) -> None:
    """Кандидат и заготовка резюме создаются, чтобы скринингу было что догружать."""
    await seed_vacancy(container)
    await seed_response(container)

    await IngestService(container).poll(Channel.HH)

    store = await store_of(container)
    candidate = next(iter(store.candidates.values()))
    assert candidate.external_id == "hh-cand-test-1"
    assert candidate.full_name == "Тестовый Кандидат"
    assert candidate.display_name == "Тестовый"

    resume = next(iter(store.resumes.values()))
    assert resume.external_id == "hh-resume-test-1"
    assert resume.raw_text == ""  # текст догружается позже: квота не тратится зря


async def test_two_responses_of_one_candidate_create_two_conversations(
    container: Container,
) -> None:
    """Один кандидат с двумя откликами — один кандидат и два диалога."""
    await seed_vacancy(container)
    await seed_response(container)
    await seed_response(
        container,
        conversation_external_id="hh-chat-2",
        received_at=FIXED_NOW + timedelta(minutes=2),
    )

    result = await IngestService(container).poll(Channel.HH)

    assert result.created == 2
    store = await store_of(container)
    assert len(store.candidates) == 1
    assert len(store.conversations) == 2


async def test_outbox_entry_is_available_immediately(container: Container) -> None:
    """Запись outbox доступна сразу: время берётся из порта часов, а не из системы."""
    await seed_pipeline(container)

    await IngestService(container).poll(Channel.HH)

    store = await store_of(container)
    entry = next(iter(store.outbox.values()))
    now = container.clock.now()
    assert entry.available_at <= now
    assert entry.created_at <= now


async def test_fake_channel_returns_only_fresh_responses(container: Container) -> None:
    """Сам fake-канал соблюдает контракт «только новые отклики»."""
    await seed_pipeline(container)
    adapter = container.channel(Channel.HH)
    assert isinstance(adapter, FakeChannelAdapter)

    first = await adapter.fetch_new_responses(limit=10)
    second = await adapter.fetch_new_responses(limit=10)

    assert len(first) == 1
    assert list(second) == []
    assert first[0].received_at == datetime(2024, 1, 1, 9, 0, tzinfo=UTC)
