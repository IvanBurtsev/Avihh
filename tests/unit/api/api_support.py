"""Дубли инфраструктуры для тестов HTTP API.

Модуль лежит рядом с тестами (не в пакете ``tests``, чтобы не заводить
``__init__.py`` в общем дереве тестов) и содержит:

* ``FakeStore`` + ``FakeUnitOfWork`` — in-memory репозитории, повторяющие
  контракты ``hirebot.ports.repositories``;
* заглушки портов (кэш, очередь, локи, часы, HTTP);
* fake-сервисы: тесты подменяют ими ``hirebot.services.*`` через
  ``app.dependency_overrides`` и проверяют аргументы вызова.

Ничего из этого не ходит в сеть, БД и Redis.
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from pydantic import SecretStr

from hirebot.config import SecuritySettings, Settings
from hirebot.container import Container
from hirebot.domain.entities import (
    Booking,
    Candidate,
    Conversation,
    InterviewSlot,
    Message,
    OutboxEntry,
    Resume,
    ScreeningCriterion,
    ScreeningResult,
    Vacancy,
)
from hirebot.domain.enums import (
    BookingStatus,
    Channel,
    ConversationState,
    MessageDirection,
    MessageKind,
    OutboxStatus,
    ScreeningVerdict,
)

COMPANY_ID = UUID("11111111-1111-1111-1111-111111111111")
MANAGER_ID = "manager-1"


# ---------------------------------------------------------------------------
# Заглушки инфраструктурных портов
# ---------------------------------------------------------------------------


class FakeClock:
    """Часы без реальных задержек."""

    def now(self) -> datetime:
        return datetime.now(UTC)

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(0)


class FakeCache:
    """Кэш с TTL-заглушкой и атомарным ``incr`` (нужен идемпотентности вебхуков)."""

    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}
        self.counters: dict[str, int] = {}

    async def get(self, key: str) -> bytes | None:
        return self.values.get(key)

    async def set(self, key: str, value: bytes, *, ttl_s: float | None = None) -> None:
        self.values[key] = value

    async def delete(self, key: str) -> None:
        self.values.pop(key, None)
        self.counters.pop(key, None)

    async def get_json(self, key: str) -> Any | None:
        raw = self.values.get(key)
        return json.loads(raw) if raw is not None else None

    async def set_json(self, key: str, value: Any, *, ttl_s: float | None = None) -> None:
        self.values[key] = json.dumps(value).encode("utf-8")

    async def incr(self, key: str, *, ttl_s: float | None = None) -> int:
        self.counters[key] = self.counters.get(key, 0) + 1
        return self.counters[key]


class FakeQueue:
    """Очередь задач: запоминает постановки, ничего не выполняет."""

    def __init__(self, *, fail: bool = False) -> None:
        self.jobs: list[dict[str, Any]] = []
        self.started = False
        self.closed = False
        self.fail = fail

    async def enqueue(
        self,
        job_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        delay_s: float = 0.0,
    ) -> str | None:
        if self.fail:
            msg = "очередь недоступна"
            raise RuntimeError(msg)
        self.jobs.append({"job": job_name, "payload": payload, "dedupe_key": dedupe_key})
        return f"job-{len(self.jobs)}"

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.closed = True


@asynccontextmanager
async def _acquire_true() -> AsyncIterator[bool]:
    yield True


class FakeLocks:
    """Блокировки всегда достаются."""

    def acquire(
        self, key: str, *, ttl_s: float = 30.0, wait_s: float = 5.0
    ) -> AbstractAsyncContextManager[bool]:
        return _acquire_true()

    async def aclose(self) -> None:
        return None


class FakeRateLimiter:
    """Лимитер без ожиданий."""

    async def acquire(self, key: str, *, rate_per_second: float, burst: int, wait: bool = True) -> float:
        return 0.0


class FakeHttpClient:
    """HTTP-клиент, который падает при любой попытке выйти в сеть."""

    async def request(self, *args: Any, **kwargs: Any) -> Any:
        msg = "unit-тест API не должен ходить в сеть"
        raise AssertionError(msg)

    async def aclose(self) -> None:
        return None


class FakeChannel:
    """Адаптер площадки: умеет ровно то, что нужно слою доставки."""

    def __init__(self, channel: Channel, *, healthy: bool = True) -> None:
        self.channel = channel
        self.healthy = healthy
        self.healthchecks = 0

    async def healthcheck(self) -> bool:
        self.healthchecks += 1
        return self.healthy

    async def list_vacancies(self) -> Sequence[Any]:
        return ()

    async def fetch_new_responses(
        self, *, vacancy_external_id: str | None = None, limit: int = 50
    ) -> Sequence[Any]:
        return ()


# ---------------------------------------------------------------------------
# In-memory хранилище и репозитории
# ---------------------------------------------------------------------------


class FakeStore:
    """Данные теста: живут между «транзакциями» fake Unit of Work."""

    def __init__(self) -> None:
        self.conversations: dict[UUID, Conversation] = {}
        self.candidates: dict[UUID, Candidate] = {}
        self.vacancies: dict[UUID, Vacancy] = {}
        self.resumes: dict[UUID, Resume] = {}
        self.messages: list[Message] = []
        self.screenings: list[ScreeningResult] = []
        self.bookings: list[Booking] = []
        self.outbox: list[OutboxEntry] = []
        self.idempotency: list[str] = []
        #: Имитация недоступной БД: любой вход в Unit of Work падает.
        self.broken = False


class _ConversationRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def upsert(self, conversation: Conversation) -> Conversation:
        self.store.conversations[conversation.id] = conversation
        return conversation

    async def get(self, conversation_id: UUID) -> Conversation | None:
        return self.store.conversations.get(conversation_id)

    async def get_with_lock(self, conversation_id: UUID) -> Conversation | None:
        return self.store.conversations.get(conversation_id)

    async def get_by_external(self, channel: Channel, external_id: str) -> Conversation | None:
        for conversation in self.store.conversations.values():
            if conversation.channel == channel and conversation.external_id == external_id:
                return conversation
        return None

    async def list_by_state(
        self, states: Sequence[ConversationState], *, limit: int = 100
    ) -> Sequence[Conversation]:
        allowed = set(states)
        found = [item for item in self.store.conversations.values() if item.state in allowed]
        return sorted(found, key=lambda item: item.created_at, reverse=True)[:limit]


class _CandidateRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def upsert(self, candidate: Candidate) -> Candidate:
        self.store.candidates[candidate.id] = candidate
        return candidate

    async def get(self, candidate_id: UUID) -> Candidate | None:
        return self.store.candidates.get(candidate_id)

    async def get_by_external(self, channel: Channel, external_id: str) -> Candidate | None:
        for candidate in self.store.candidates.values():
            if candidate.channel == channel and candidate.external_id == external_id:
                return candidate
        return None


class _VacancyRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def upsert(self, vacancy: Vacancy) -> Vacancy:
        self.store.vacancies[vacancy.id] = vacancy
        return vacancy

    async def get(self, vacancy_id: UUID) -> Vacancy | None:
        return self.store.vacancies.get(vacancy_id)

    async def get_by_external(self, channel: Channel, external_id: str) -> Vacancy | None:
        for vacancy in self.store.vacancies.values():
            if vacancy.channel == channel and vacancy.external_id == external_id:
                return vacancy
        return None

    async def list_active(self, company_id: UUID) -> Sequence[Vacancy]:
        return [item for item in self.store.vacancies.values() if item.company_id == company_id]


class _ResumeRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def save(self, resume: Resume) -> Resume:
        self.store.resumes[resume.id] = resume
        return resume

    async def get(self, resume_id: UUID) -> Resume | None:
        return self.store.resumes.get(resume_id)

    async def get_latest_for_candidate(self, candidate_id: UUID) -> Resume | None:
        items = [item for item in self.store.resumes.values() if item.candidate_id == candidate_id]
        return max(items, key=lambda item: item.fetched_at) if items else None


class _MessageRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def add(self, message: Message) -> Message:
        self.store.messages.append(message)
        return message

    async def list_for_conversation(self, conversation_id: UUID, *, limit: int = 50) -> Sequence[Message]:
        found = [item for item in self.store.messages if item.conversation_id == conversation_id]
        return found[-limit:]

    async def exists_external_id(self, conversation_id: UUID, external_id: str) -> bool:
        return any(
            item.conversation_id == conversation_id and item.external_id == external_id
            for item in self.store.messages
        )


class _ScreeningRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def save(self, result: ScreeningResult) -> ScreeningResult:
        self.store.screenings.append(result)
        return result

    async def get_latest(self, candidate_id: UUID, vacancy_id: UUID) -> ScreeningResult | None:
        items = [
            item
            for item in self.store.screenings
            if item.candidate_id == candidate_id and item.vacancy_id == vacancy_id
        ]
        return max(items, key=lambda item: item.created_at) if items else None

    async def get_by_content_hash(self, content_hash: str) -> ScreeningResult | None:
        return None


class _BookingRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def save(self, booking: Booking) -> Booking:
        self.store.bookings.append(booking)
        return booking

    async def get(self, booking_id: UUID) -> Booking | None:
        for booking in self.store.bookings:
            if booking.id == booking_id:
                return booking
        return None

    async def get_by_idempotency_key(self, key: str) -> Booking | None:
        for booking in self.store.bookings:
            if booking.idempotency_key == key:
                return booking
        return None

    async def list_active_for_manager(
        self, manager_id: str, *, start: datetime, end: datetime
    ) -> Sequence[Booking]:
        return [
            item
            for item in self.store.bookings
            if item.manager_id == manager_id and item.start < end and start < item.end
        ]

    async def get_for_conversation(self, conversation_id: UUID) -> Booking | None:
        """Необязательная возможность порта: карточка диалога показывает бронь."""
        for booking in self.store.bookings:
            if booking.conversation_id == conversation_id:
                return booking
        return None


class _OutboxRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def add(self, entry: OutboxEntry) -> OutboxEntry:
        self.store.outbox.append(entry)
        return entry

    async def claim_batch(self, *, limit: int, now: datetime) -> Sequence[OutboxEntry]:
        pending = [item for item in self.store.outbox if item.status == OutboxStatus.PENDING]
        return pending[:limit]

    async def mark(self, entry_id: UUID, status: OutboxStatus, *, error: str | None = None) -> None:
        for entry in self.store.outbox:
            if entry.id == entry_id:
                entry.status = status
                entry.last_error = error


class _IdempotencyRepo:
    def __init__(self, store: FakeStore) -> None:
        self.store = store

    async def acquire(self, key: str, *, scope: str, ttl_hours: int = 72) -> bool:
        compound = f"{scope}:{key}"
        if compound in self.store.idempotency:
            return False
        self.store.idempotency.append(compound)
        return True


class FakeUnitOfWork:
    """Unit of Work поверх ``FakeStore``."""

    def __init__(self, store: FakeStore) -> None:
        self.store = store
        self.commits = 0
        self.rollbacks = 0
        self.conversations = _ConversationRepo(store)
        self.candidates = _CandidateRepo(store)
        self.vacancies = _VacancyRepo(store)
        self.resumes = _ResumeRepo(store)
        self.messages = _MessageRepo(store)
        self.screenings = _ScreeningRepo(store)
        self.bookings = _BookingRepo(store)
        self.outbox = _OutboxRepo(store)
        self.idempotency = _IdempotencyRepo(store)

    async def __aenter__(self) -> FakeUnitOfWork:
        if self.store.broken:
            msg = "база данных недоступна"
            raise RuntimeError(msg)
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        return None

    async def commit(self) -> None:
        self.commits += 1

    async def rollback(self) -> None:
        self.rollbacks += 1


# ---------------------------------------------------------------------------
# Сборка контейнера и тестовых данных
# ---------------------------------------------------------------------------


def make_settings(*, webhook_secret: str | None = None, env: str = "test") -> Settings:
    """Настройки теста: локальное окружение, по умолчанию без секретов."""
    return Settings(
        env=env,  # type: ignore[arg-type]
        log_level="WARNING",
        log_json=False,
        security=SecuritySettings(
            webhook_secret=SecretStr(webhook_secret) if webhook_secret is not None else None
        ),
    )


def make_container(
    settings: Settings | None = None,
    *,
    store: FakeStore | None = None,
    channels: Mapping[Channel, FakeChannel] | None = None,
    queue: FakeQueue | None = None,
    cache: FakeCache | None = None,
) -> Container:
    """Собрать контейнер на заглушках."""
    resolved_store = store if store is not None else FakeStore()
    resolved_channels: Mapping[Channel, FakeChannel] = (
        channels if channels is not None else {Channel.HH: FakeChannel(Channel.HH)}
    )
    return Container(
        settings=settings if settings is not None else make_settings(),
        uow_factory=lambda: FakeUnitOfWork(resolved_store),
        http=FakeHttpClient(),  # type: ignore[arg-type]
        cache=(cache if cache is not None else FakeCache()),  # type: ignore[arg-type]
        locks=FakeLocks(),  # type: ignore[arg-type]
        rate_limiter=FakeRateLimiter(),  # type: ignore[arg-type]
        queue=(queue if queue is not None else FakeQueue()),  # type: ignore[arg-type]
        clock=FakeClock(),  # type: ignore[arg-type]
        llm=object(),
        cipher=object(),
        channels=resolved_channels,  # type: ignore[arg-type]
        calendar=None,
    )


def seed_conversation(
    store: FakeStore,
    *,
    state: ConversationState = ConversationState.NEW,
    channel: Channel = Channel.HH,
    full_name: str = "Иван Иванов",
    phone: str | None = "+7 999 123-45-67",
    email: str | None = "ivan@example.com",
    external_id: str = "conv-1",
) -> Conversation:
    """Создать кандидата, вакансию и диалог в хранилище."""
    candidate = Candidate(
        id=uuid4(),
        channel=channel,
        external_id=f"cand-{external_id}",
        full_name=full_name,
        company_id=COMPANY_ID,
        phone=phone,
        email=email,
        city="Москва",
    )
    vacancy = Vacancy(
        id=uuid4(),
        channel=channel,
        external_id=f"vac-{external_id}",
        title="Python-разработчик",
        company_id=COMPANY_ID,
        city="Москва",
    )
    conversation = Conversation(
        id=uuid4(),
        company_id=COMPANY_ID,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        channel=channel,
        external_id=external_id,
        state=state,
    )
    store.candidates[candidate.id] = candidate
    store.vacancies[vacancy.id] = vacancy
    store.conversations[conversation.id] = conversation
    return conversation


def seed_screening(store: FakeStore, conversation: Conversation, *, score: float = 82.0) -> ScreeningResult:
    """Добавить результат скрининга по диалогу."""
    result = ScreeningResult(
        id=uuid4(),
        resume_id=uuid4(),
        vacancy_id=conversation.vacancy_id,
        candidate_id=conversation.candidate_id,
        total_score=score,
        verdict=ScreeningVerdict.MATCH,
        criteria=[
            ScreeningCriterion(
                key="python",
                title="Python",
                weight=1.0,
                score=90.0,
                must_have=True,
                evidence="5 лет на Python",
            )
        ],
        summary="Подходит",
        gaps=[],
        strengths=["Опыт"],
        questions=["Когда готовы начать?"],
        model="fake",
    )
    store.screenings.append(result)
    return result


def seed_message(store: FakeStore, conversation: Conversation, *, body: str = "Здравствуйте!") -> Message:
    """Добавить сообщение в переписку диалога."""
    message = Message(
        id=uuid4(),
        conversation_id=conversation.id,
        direction=MessageDirection.OUTBOUND,
        body=body,
        kind=MessageKind.GREETING,
    )
    store.messages.append(message)
    return message


def make_slot(*, start_offset_hours: int = 24) -> InterviewSlot:
    """Свободный слот для ответов ``/slots``."""
    start = datetime.now(UTC) + timedelta(hours=start_offset_hours)
    return InterviewSlot(
        start=start,
        end=start + timedelta(minutes=60),
        manager_id=MANAGER_ID,
        timezone="Europe/Moscow",
    )


def make_booking(conversation: Conversation, *, status: BookingStatus = BookingStatus.CONFIRMED) -> Booking:
    """Бронь по диалогу (для карточки диалога)."""
    slot = make_slot()
    return Booking(
        id=uuid4(),
        conversation_id=conversation.id,
        candidate_id=conversation.candidate_id,
        vacancy_id=conversation.vacancy_id,
        manager_id=MANAGER_ID,
        start=slot.start,
        end=slot.end,
        timezone=slot.timezone,
        status=status,
        external_meeting_id="meeting-1",
    )


# ---------------------------------------------------------------------------
# Fake-сервисы
# ---------------------------------------------------------------------------


@dataclass
class FakePollResult:
    """Похож на ``PollResult`` сервисного слоя: счётчики и список исходов."""

    fetched: int = 3
    created: int = 2
    skipped: int = 1
    outcomes: list[str] = field(default_factory=lambda: ["conv-1"])


@dataclass
class FakeConversationOutcome:
    """Похож на ``ConversationOutcome``: состояние и счётчики."""

    state: ConversationState = ConversationState.QUESTIONS_SENT
    steps: int = 3
    sent: int = 1


@dataclass
class FakeIngestService:
    """Замена ``IngestService``: пишет вызовы и возвращает заданный результат."""

    result: Any = field(default_factory=FakePollResult)
    error: Exception | None = None
    calls: list[tuple[Channel, int]] = field(default_factory=list)

    async def poll(self, channel: Channel, *, limit: int = 50) -> Any:
        self.calls.append((channel, limit))
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeConversationService:
    """Замена ``ConversationService``."""

    result: Any = field(default_factory=FakeConversationOutcome)
    error: Exception | None = None
    calls: list[UUID] = field(default_factory=list)

    async def process(self, conversation_id: UUID) -> Any:
        self.calls.append(conversation_id)
        if self.error is not None:
            raise self.error
        return self.result


@dataclass
class FakeSchedulingService:
    """Замена ``SchedulingService``."""

    slots: list[Any] = field(default_factory=list)
    booking: Any = None
    error: Exception | None = None
    slot_calls: list[dict[str, Any]] = field(default_factory=list)
    book_calls: list[dict[str, Any]] = field(default_factory=list)

    async def propose_slots(self, *, conversation_id: UUID, days_ahead: int = 5, limit: int = 3) -> list[Any]:
        self.slot_calls.append({"conversation_id": conversation_id, "days_ahead": days_ahead, "limit": limit})
        if self.error is not None:
            raise self.error
        return self.slots

    async def book(
        self,
        *,
        conversation_id: UUID,
        slot_index: int | None = None,
        slot_start: Any = None,
        requested_by: str = "bot",
    ) -> Any:
        self.book_calls.append(
            {
                "conversation_id": conversation_id,
                "slot_index": slot_index,
                "slot_start": slot_start,
                "requested_by": requested_by,
            }
        )
        if self.error is not None:
            raise self.error
        return self.booking


@dataclass
class FakeServices:
    """Набор подменяемых сервисов."""

    ingest: FakeIngestService = field(default_factory=FakeIngestService)
    conversation: FakeConversationService = field(default_factory=FakeConversationService)
    scheduling: FakeSchedulingService = field(default_factory=FakeSchedulingService)


# ---------------------------------------------------------------------------
# Вспомогательные контекстные менеджеры
# ---------------------------------------------------------------------------


@asynccontextmanager
async def asgi_client(app: FastAPI, *, raise_app_exceptions: bool = True) -> AsyncIterator[AsyncClient]:
    """HTTP-клиент поверх ASGI-транспорта: без сети и без запуска сервера.

    ``raise_app_exceptions=False`` нужен там, где проверяется обработчик
    необработанных исключений: starlette перебрасывает исключение после ответа.
    """
    transport = ASGITransport(app=app, raise_app_exceptions=raise_app_exceptions)
    async with AsyncClient(transport=transport, base_url="http://test") as http_client:
        yield http_client


__all__ = [
    "COMPANY_ID",
    "MANAGER_ID",
    "FakeCache",
    "FakeChannel",
    "FakeClock",
    "FakeConversationOutcome",
    "FakeConversationService",
    "FakeHttpClient",
    "FakeIngestService",
    "FakeLocks",
    "FakePollResult",
    "FakeQueue",
    "FakeRateLimiter",
    "FakeSchedulingService",
    "FakeServices",
    "FakeStore",
    "FakeUnitOfWork",
    "asgi_client",
    "make_booking",
    "make_container",
    "make_settings",
    "make_slot",
    "seed_conversation",
    "seed_message",
    "seed_screening",
]
