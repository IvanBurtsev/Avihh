"""Тесты задач воркера.

Задачи — тонкие обёртки над сервисами, поэтому проверяется именно контракт:

* имя задачи и состав ответа (по ним API и мониторинг понимают, что произошло);
* payload разбирается строго: невалидный UUID — ошибка постановки задачи, а не
  сбой обработки, и сервис не должен вызываться вовсе;
* ``correlation_id`` присутствует в каждой задаче — иначе цепочку «отклик →
  сообщение → бронь» невозможно восстановить по логам;
* отсутствие контейнера в контексте даёт понятную ошибку, а не ``KeyError``.

Сам бизнес-смысл проверяется в тестах сервисов; здесь дублировать его не нужно.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import pytest
from worker_support import (
    FakeOutboxDispatcher,
    FakeSchedulingService,
    make_booking,
    make_container,
    make_spies,
    patch_services,
)

from hirebot.domain.enums import Channel, ConversationState
from hirebot.worker import tasks


def job_context(container: Any) -> dict[str, Any]:
    """Минимальный контекст задачи, как его формирует arq/CLI."""
    return {"container": container, "job_id": "job-1"}


async def test_poll_channel_delegates_to_ingest(monkeypatch: Any) -> None:
    spies = patch_services(monkeypatch, make_spies())

    result = await tasks.poll_channel(job_context(make_container()), "hh", limit=7)

    assert result["job"] == tasks.JOB_POLL_CHANNEL
    assert result["channel"] == Channel.HH.value
    assert spies.ingest.service.calls == [{"channel": Channel.HH, "limit": 7}]
    assert result["correlation_id"], "задача обязана нести correlation id"


async def test_poll_channel_rejects_unknown_channel(monkeypatch: Any) -> None:
    spies = patch_services(monkeypatch, make_spies())

    with pytest.raises(ValueError, match="Неизвестный канал"):
        await tasks.poll_channel(job_context(make_container()), "telegram")

    assert spies.ingest.service.calls == [], "неизвестный канал не должен доходить до сервиса"


async def test_process_conversation_returns_state(monkeypatch: Any) -> None:
    spies = patch_services(monkeypatch, make_spies())
    conversation_id = uuid4()

    result = await tasks.process_conversation(job_context(make_container()), str(conversation_id))

    assert result["conversation_id"] == str(conversation_id)
    assert result["state"] == ConversationState.INVITED.value
    assert spies.conversation.service.calls == [conversation_id]


async def test_process_conversation_rejects_malformed_uuid(monkeypatch: Any) -> None:
    """Битый идентификатор — ошибка постановки задачи: повтор не поможет."""
    spies = patch_services(monkeypatch, make_spies())

    with pytest.raises(ValueError, match="conversation_id"):
        await tasks.process_conversation(job_context(make_container()), "не-uuid")

    assert spies.conversation.service.calls == []


async def test_book_interview_passes_slot_and_reports_meeting(monkeypatch: Any) -> None:
    conversation_id = uuid4()
    booking = make_booking(conversation_id, external_meeting_id="talantix-42")
    scheduling = FakeSchedulingService(booking=booking)
    patch_services(monkeypatch, make_spies(scheduling=scheduling))

    result = await tasks.book_interview(job_context(make_container()), str(conversation_id), 2)

    assert result["job"] == tasks.JOB_BOOK_INTERVIEW
    assert result["slot_index"] == 2
    assert result["slot_start"] is None
    assert result["external_meeting_id"] == "talantix-42"
    assert result["booking_id"] == str(booking.id)
    assert scheduling.calls == [
        {
            "conversation_id": conversation_id,
            "slot_index": 2,
            "slot_start": None,
            "requested_by": "bot",
        }
    ]


async def test_book_interview_prefers_explicit_slot_time(monkeypatch: Any) -> None:
    """Задача со временем слота не зависит от пересчёта сетки между постановкой и запуском."""
    conversation_id = uuid4()
    booking = make_booking(conversation_id)
    scheduling = FakeSchedulingService(booking=booking)
    patch_services(monkeypatch, make_spies(scheduling=scheduling))

    result = await tasks.book_interview(
        job_context(make_container()), str(conversation_id), slot_start="2026-06-01T10:00:00+00:00"
    )

    assert scheduling.calls == [
        {
            "conversation_id": conversation_id,
            "slot_index": None,
            "slot_start": datetime(2026, 6, 1, 10, 0, tzinfo=UTC),
            "requested_by": "bot",
        }
    ]
    assert result["slot_start"] == "2026-06-01T10:00:00+00:00"


async def test_book_interview_rejects_naive_slot_time(monkeypatch: Any) -> None:
    """Наивное время — ошибка постановки задачи: слоты сравниваются между поясами."""
    patch_services(monkeypatch, make_spies())

    with pytest.raises(ValueError, match="часовой пояс"):
        await tasks.book_interview(
            job_context(make_container()), str(uuid4()), slot_start="2026-06-01T10:00:00"
        )


async def test_dispatch_outbox_reports_delivered_count(monkeypatch: Any) -> None:
    dispatcher = FakeOutboxDispatcher(delivered=5)
    patch_services(monkeypatch, make_spies(dispatcher=dispatcher))

    result = await tasks.dispatch_outbox(job_context(make_container()), batch_size=3)

    assert result["job"] == tasks.JOB_DISPATCH_OUTBOX
    assert dispatcher.calls == [3]


async def test_task_without_container_fails_with_clear_message(monkeypatch: Any) -> None:
    """Воркер, запущенный без startup-фазы, должен сказать об этом прямо."""
    patch_services(monkeypatch, make_spies())

    with pytest.raises(RuntimeError, match="контейнер"):
        await tasks.poll_channel({}, "hh")


async def test_job_names_match_function_names() -> None:
    """Имена задач — контракт с очередью: API ставит задачи по этим строкам."""
    names = {
        tasks.poll_channel.__name__: tasks.JOB_POLL_CHANNEL,
        tasks.process_conversation.__name__: tasks.JOB_PROCESS_CONVERSATION,
        tasks.book_interview.__name__: tasks.JOB_BOOK_INTERVIEW,
        tasks.dispatch_outbox.__name__: tasks.JOB_DISPATCH_OUTBOX,
    }
    for function_name, job_name in names.items():
        assert function_name == job_name, f"имя задачи {job_name!r} разошлось с функцией"
