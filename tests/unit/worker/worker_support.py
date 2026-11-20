"""Дубли инфраструктуры для тестов воркера.

Задачам воркера от контейнера нужны только очередь, часы и список включённых
каналов, поэтому заглушки здесь минимальные: сеть, БД и Redis не участвуют.
"""

from __future__ import annotations

import asyncio
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from hirebot.config import Settings
from hirebot.container import Container
from hirebot.domain.entities import Booking, InterviewSlot
from hirebot.domain.enums import Channel, ConversationState

MANAGER_ID = "manager-1"


class StubClock:
    """Часы без задержек."""

    def __init__(self) -> None:
        self.slept: list[float] = []

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        await asyncio.sleep(0)


class StubQueue:
    """Очередь-заглушка: в тестах задач воркера не используется."""

    async def enqueue(
        self,
        job_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        delay_s: float = 0.0,
    ) -> str | None:
        return None

    async def start(self) -> None:
        return None

    async def close(self) -> None:
        return None


class StubHttp:
    """HTTP-клиент: запоминает закрытие, в сеть не ходит."""

    def __init__(self) -> None:
        self.closed = False

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        msg = "unit-тест воркера не должен ходить в сеть"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        self.closed = True


class StubChannel:
    """Адаптер канала: воркеру достаточно факта наличия."""

    def __init__(self, channel: Channel) -> None:
        self.channel = channel

    async def healthcheck(self) -> bool:
        return True


class StubCache:
    """Кэш-заглушка."""

    async def get(self, key: str) -> bytes | None:
        return None

    async def set(self, key: str, value: bytes, *, ttl_s: float | None = None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None


def _unused_uow() -> Any:
    msg = "unit-тест воркера не должен открывать Unit of Work"
    raise AssertionError(msg)


def make_settings() -> Settings:
    """Настройки локального окружения для тестов воркера."""
    return Settings(env="test", log_level="WARNING", log_json=False)


def make_container(
    settings: Settings | None = None,
    *,
    channels: Mapping[Channel, StubChannel] | None = None,
    clock: StubClock | None = None,
) -> Container:
    """Контейнер на заглушках: только то, что нужно задачам."""
    resolved_channels: Mapping[Channel, StubChannel] = (
        channels if channels is not None else {Channel.HH: StubChannel(Channel.HH)}
    )
    return Container(
        settings=settings if settings is not None else make_settings(),
        uow_factory=_unused_uow,
        http=StubHttp(),  # type: ignore[arg-type]
        cache=StubCache(),  # type: ignore[arg-type]
        locks=object(),  # type: ignore[arg-type]
        rate_limiter=object(),  # type: ignore[arg-type]
        queue=StubQueue(),  # type: ignore[arg-type]
        clock=(clock if clock is not None else StubClock()),  # type: ignore[arg-type]
        llm=object(),
        cipher=object(),
        channels=resolved_channels,  # type: ignore[arg-type]
        calendar=None,
    )


# ---------------------------------------------------------------------------
# Fake-сервисы и подмена фабрик
# ---------------------------------------------------------------------------


@dataclass
class PollOutcome:
    """Похож на ``PollResult`` сервисного слоя."""

    fetched: int = 4
    created: int = 3
    errors: list[str] = field(default_factory=lambda: ["one failed"])


@dataclass
class ConversationOutcome:
    """Похож на ``ConversationOutcome``."""

    state: ConversationState = ConversationState.INVITED
    steps: int = 2


@dataclass
class FakeIngestService:
    result: Any = field(default_factory=PollOutcome)
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def poll(self, channel: Channel, *, limit: int = 50) -> Any:
        self.calls.append({"channel": channel, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeConversationService:
    result: Any = field(default_factory=ConversationOutcome)
    error: Exception | None = None
    calls: list[UUID] = field(default_factory=list)

    async def process(self, conversation_id: UUID) -> Any:
        self.calls.append(conversation_id)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeSchedulingService:
    booking: Any = None
    error: Exception | None = None
    calls: list[dict[str, Any]] = field(default_factory=list)

    async def propose_slots(
        self, *, conversation_id: UUID, days_ahead: int = 5, limit: int = 3
    ) -> list[InterviewSlot]:
        return []

    async def book(
        self,
        *,
        conversation_id: UUID,
        slot_index: int | None = None,
        slot_start: Any = None,
        requested_by: str = "bot",
    ) -> Any:
        self.calls.append(
            {
                "conversation_id": conversation_id,
                "slot_index": slot_index,
                "slot_start": slot_start,
                "requested_by": requested_by,
            }
        )
        if self.error is not None:
            raise self.error
        return self.booking if self.booking is not None else make_booking(conversation_id)


@dataclass
class FakeOutboxDispatcher:
    delivered: int = 2
    error: Exception | None = None
    calls: list[int] = field(default_factory=list)

    async def drain(self, *, batch_size: int = 20) -> int:
        self.calls.append(batch_size)
        if self.error is not None:
            raise self.error
        return self.delivered


def make_booking(conversation_id: UUID, *, external_meeting_id: str = "meeting-1") -> Booking:
    """Бронь для проверки результата задачи ``book_interview``."""
    start = datetime.now(UTC)
    return Booking(
        id=uuid4(),
        conversation_id=conversation_id,
        candidate_id=uuid4(),
        vacancy_id=uuid4(),
        manager_id=MANAGER_ID,
        start=start,
        end=start + timedelta(minutes=60),
        timezone="Europe/Moscow",
        external_meeting_id=external_meeting_id,
    )


class ServiceSpy:
    """Замена фабрики сервиса: запоминает контейнер и отдаёт fake-сервис."""

    def __init__(self, service: Any) -> None:
        self.service = service
        self.containers: list[Container] = []

    def __call__(self, container: Container) -> Any:
        self.containers.append(container)
        return self.service


@dataclass
class Spies:
    """Все подменённые фабрики сервисов."""

    ingest: ServiceSpy
    conversation: ServiceSpy
    scheduling: ServiceSpy
    dispatcher: ServiceSpy


def make_spies(
    *,
    ingest: FakeIngestService | None = None,
    conversation: FakeConversationService | None = None,
    scheduling: FakeSchedulingService | None = None,
    dispatcher: FakeOutboxDispatcher | None = None,
) -> Spies:
    """Собрать набор подмен для ``hirebot.worker.services``."""
    return Spies(
        ingest=ServiceSpy(ingest if ingest is not None else FakeIngestService()),
        conversation=ServiceSpy(conversation if conversation is not None else FakeConversationService()),
        scheduling=ServiceSpy(scheduling if scheduling is not None else FakeSchedulingService()),
        dispatcher=ServiceSpy(dispatcher if dispatcher is not None else FakeOutboxDispatcher()),
    )


def patch_services(monkeypatch: Any, spies: Spies) -> Spies:
    """Подменить фабрики сервисов воркера."""
    from hirebot.worker import services

    monkeypatch.setattr(services, "ingest_service", spies.ingest)
    monkeypatch.setattr(services, "conversation_service", spies.conversation)
    monkeypatch.setattr(services, "scheduling_service", spies.scheduling)
    monkeypatch.setattr(services, "outbox_dispatcher", spies.dispatcher)
    return spies


def empty_channels() -> Sequence[Channel]:
    return ()


__all__ = [
    "FakeConversationService",
    "FakeIngestService",
    "FakeOutboxDispatcher",
    "FakeSchedulingService",
    "ServiceSpy",
    "Spies",
    "StubCache",
    "StubChannel",
    "StubClock",
    "StubHttp",
    "StubQueue",
    "make_booking",
    "make_container",
    "make_settings",
    "make_spies",
    "patch_services",
]
