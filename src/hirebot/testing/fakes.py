"""Fake-реализации для тестов и демо-прогона.

Ничего в этом модуле не ходит в сеть, не поднимает БД и не требует ключей:
именно поэтому демонстрационный прогон воронки возможен «из коробки»
(ARCHITECTURE.md, п. 2.6).

Что здесь есть:

* ``InMemoryUnitOfWork`` — полноценный ``UnitOfWork`` на словарях, включая
  ``get_with_lock`` и ``idempotency.acquire``, со снимком состояния и
  откатом (``rollback`` восстанавливает данные);
* ``FakeChannelAdapter`` — площадка в памяти: вакансии, отклики, чаты,
  идемпотентная отправка сообщений;
* ``FakeCalendarAdapter`` — задаваемая занятость и создаваемые встречи,
  с возможностью «потерять» создание или перечитывание (таймаут);
* ``FakeLlmAdapter`` — детерминированная «модель»: считает баллы по фактам из
  резюме (годы опыта, ключевые слова) и пишет человекоподобные сообщения
  строго по переданным данным. Ни одного выдуманного факта;
* ``build_fake_container`` — сборка ``Container`` на этих fake-реализациях.
"""

from __future__ import annotations

import copy
import importlib
import re
from collections.abc import AsyncIterator, Mapping, Sequence
from contextlib import AbstractAsyncContextManager, asynccontextmanager
from dataclasses import dataclass, field, fields, replace
from datetime import UTC, datetime, timedelta
from typing import Any, Self, cast
from uuid import UUID

from hirebot.config import Settings
from hirebot.container import Container, UowFactory
from hirebot.domain.budget import PaidAction
from hirebot.domain.entities import (
    Booking,
    BusyInterval,
    Candidate,
    CandidateReply,
    Company,
    Conversation,
    DraftMessage,
    InterviewSlot,
    Message,
    OutboxEntry,
    QuestionAnswer,
    Resume,
    ScreeningCriterion,
    ScreeningResult,
    Vacancy,
    new_id,
)
from hirebot.domain.enums import (
    BookingStatus,
    CandidateIntent,
    Channel,
    ConversationState,
    DraftStatus,
    MessageDirection,
    OutboxStatus,
    ProfileStatus,
)
from hirebot.domain.errors import (
    NotFound,
    ResumeAccessDenied,
    SlotConflict,
)
from hirebot.domain.scoring import ScoringProfile
from hirebot.infra.cache import MemoryCache
from hirebot.logging_setup import get_logger
from hirebot.ports.dto import (
    ChannelAction,
    ConversationSnapshot,
    InboundMessage,
    InboundResponse,
    ResumePayload,
    SentMessage,
    VacancyPayload,
)
from hirebot.ports.infra import HttpResponse
from hirebot.ports.llm import (
    AnswerAssessment,
    AnswerAssessmentRequest,
    MessageDraft,
    MessageRequest,
    ReplyAnalysisRequest,
    ScreeningDraft,
    ScreeningRequest,
)
from hirebot.ports.repositories import PurgeStats, UnitOfWork
from hirebot.services.base import DEFAULT_COMPANY_ID
from hirebot.services.outbox import backoff_delay_s
from hirebot.services.policy import normalize_text

logger = get_logger(__name__)

ANYS = Any  # псевдоним, чтобы не тащить ``Any`` в аннотации повсюду

# --------------------------------------------------------------------------- #
# Демо-данные
# --------------------------------------------------------------------------- #

DEMO_VACANCY_EXTERNAL_ID = "hh-vacancy-python-001"

DEMO_VACANCY_DESCRIPTION = (
    "Ищем backend-разработчика на Python в продуктовую команду. "
    "Задачи: развитие сервисов на FastAPI, проектирование REST API, работа с PostgreSQL. "
    "Условия: удалённый формат, гибкое начало дня, оформление по ТК РФ, "
    "обучение за счёт компании. Собеседование — один технический диалог на 60 минут."
)

DEMO_CRITERIA: tuple[dict[str, Any], ...] = (
    {
        "key": "python",
        "title": "Опыт разработки на Python",
        "weight": 3.0,
        "must_have": True,
        "expectation": "не менее 3 лет коммерческой разработки на Python",
    },
    {
        "key": "sql",
        "title": "Работа с реляционными БД (PostgreSQL/SQL)",
        "weight": 2.0,
        "must_have": False,
        "expectation": "уверенное владение SQL",
    },
    {
        "key": "api",
        "title": "Проектирование REST API",
        "weight": 1.5,
        "must_have": False,
        "expectation": "опыт проектирования и поддержки REST API",
    },
    {
        "key": "english",
        "title": "Английский язык для чтения документации",
        "weight": 1.0,
        "must_have": False,
        "expectation": "уровень B1 и выше",
    },
)

DEMO_RESUME_STRONG = (
    "Алексей Петров, backend-разработчик. Опыт работы: 5 лет, пишу на Python.\n"
    "С 2019 по 2024 год — backend-разработчик в ООО «Ромашка».\n"
    "Стек: Python 3.12, FastAPI, Django, PostgreSQL, SQL, REST API, Docker, Git, pytest.\n"
    "Проектировал и поддерживал REST API высоконагруженного сервиса, оптимизировал SQL-запросы.\n"
    "Английский язык — B2: читаю документацию и пишу письма без переводчика.\n"
    "Контакты: alexey.petrov@example.com, +7 999 123-45-67."
)

DEMO_RESUME_WEAK = (
    "Мария Смирнова, junior Python-разработчик. Опыт работы: 1 год.\n"
    "Писала учебные проекты на Python, немного знакома с SQL.\n"
    "Английский язык — A2, читаю со словарём."
)

DEMO_RESUME_DENIED = "Иван Кузнецов, системный администратор. Опыт работы: 8 лет."


def demo_responses() -> list[InboundResponse]:
    """Три отклика для демонстрации: сильный, слабый и с закрытым резюме."""
    received = datetime(2026, 6, 1, 8, 30, tzinfo=UTC)
    return [
        InboundResponse(
            channel=Channel.HH,
            conversation_external_id="hh-chat-1001",
            candidate_external_id="hh-candidate-1001",
            vacancy_external_id=DEMO_VACANCY_EXTERNAL_ID,
            candidate_name="Алексей Петров",
            resume_external_id="hh-resume-1001",
            received_at=received,
            cover_letter="Здравствуйте! Заинтересовала вакансия, готов обсудить детали.",
        ),
        InboundResponse(
            channel=Channel.HH,
            conversation_external_id="hh-chat-1002",
            candidate_external_id="hh-candidate-1002",
            vacancy_external_id=DEMO_VACANCY_EXTERNAL_ID,
            candidate_name="Мария Смирнова",
            resume_external_id="hh-resume-1002",
            received_at=received + timedelta(minutes=5),
        ),
        InboundResponse(
            channel=Channel.HH,
            conversation_external_id="hh-chat-1003",
            candidate_external_id="hh-candidate-1003",
            vacancy_external_id=DEMO_VACANCY_EXTERNAL_ID,
            candidate_name="Иван Кузнецов",
            resume_external_id="hh-resume-1003",
            received_at=received + timedelta(minutes=10),
        ),
    ]


# --------------------------------------------------------------------------- #
# In-memory хранилище и Unit of Work
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class InMemoryStore:
    """Все «таблицы» тестового контура.

    Обычные словари: транзакция — это снимок состояния, откат — восстановление
    снимка. Этого достаточно, чтобы проверить транзакционность сценариев
    (сообщение + состояние + outbox) без настоящей БД.
    """

    companies: dict[UUID, Company] = field(default_factory=dict)
    profiles: dict[UUID, ScoringProfile] = field(default_factory=dict)
    paid_actions: dict[UUID, PaidAction] = field(default_factory=dict)
    answers: dict[UUID, QuestionAnswer] = field(default_factory=dict)
    company_slugs: dict[str, UUID] = field(default_factory=dict)
    vacancies: dict[UUID, Vacancy] = field(default_factory=dict)
    vacancy_ids: dict[tuple[Channel, str], UUID] = field(default_factory=dict)
    candidates: dict[UUID, Candidate] = field(default_factory=dict)
    candidate_ids: dict[tuple[Channel, str], UUID] = field(default_factory=dict)
    resumes: dict[UUID, Resume] = field(default_factory=dict)
    conversations: dict[UUID, Conversation] = field(default_factory=dict)
    conversation_ids: dict[tuple[Channel, str], UUID] = field(default_factory=dict)
    messages: dict[UUID, Message] = field(default_factory=dict)
    message_external_ids: dict[tuple[UUID, str], UUID] = field(default_factory=dict)
    drafts: dict[UUID, DraftMessage] = field(default_factory=dict)
    screenings: dict[UUID, ScreeningResult] = field(default_factory=dict)
    screening_hashes: dict[str, list[UUID]] = field(default_factory=dict)
    # Индексы для поиска: без них каждый запрос перебирает все записи.
    messages_by_conversation: dict[UUID, list[UUID]] = field(default_factory=dict)
    screenings_by_candidate: dict[tuple[UUID, UUID], list[UUID]] = field(default_factory=dict)
    resumes_by_candidate: dict[UUID, list[UUID]] = field(default_factory=dict)
    bookings: dict[UUID, Booking] = field(default_factory=dict)
    booking_keys: dict[str, UUID] = field(default_factory=dict)
    outbox: dict[UUID, OutboxEntry] = field(default_factory=dict)
    outbox_keys: dict[str, UUID] = field(default_factory=dict)
    claimed_outbox: set[UUID] = field(default_factory=set)
    idempotency: dict[tuple[str, str], str] = field(default_factory=dict)


def _restore_store(target: InMemoryStore, source: InMemoryStore) -> None:
    """Заменить содержимое хранилища снимком (откат транзакции)."""
    for field_info in fields(InMemoryStore):
        setattr(target, field_info.name, getattr(source, field_info.name))


class InMemoryCompanyRepository:
    """Репозиторий компаний на словарях."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def upsert(self, company: Company) -> Company:
        existing_id = self._store.company_slugs.get(company.slug)
        if existing_id is not None and existing_id != company.id:
            # Дедупликация по ``slug`` — как уникальный индекс в БД.
            return self._store.companies[existing_id]
        self._store.companies[company.id] = company
        self._store.company_slugs[company.slug] = company.id
        return company

    async def get(self, company_id: UUID) -> Company | None:
        return self._store.companies.get(company_id)

    async def get_by_slug(self, slug: str) -> Company | None:
        company_id = self._store.company_slugs.get(slug)
        return self._store.companies.get(company_id) if company_id is not None else None


class InMemoryVacancyRepository:
    """Репозиторий вакансий на словарях."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def upsert(self, vacancy: Vacancy) -> Vacancy:
        key = (vacancy.channel, vacancy.external_id)
        existing_id = self._store.vacancy_ids.get(key)
        if existing_id is not None and existing_id != vacancy.id:
            # Уникальный ключ (channel, external_id): идентификатор сохраняем,
            # иначе поехали бы все ссылки из диалогов.
            vacancy = replace(vacancy, id=existing_id)
        self._store.vacancies[vacancy.id] = vacancy
        self._store.vacancy_ids[key] = vacancy.id
        return vacancy

    async def get(self, vacancy_id: UUID) -> Vacancy | None:
        return self._store.vacancies.get(vacancy_id)

    async def get_by_external(self, channel: Channel, external_id: str) -> Vacancy | None:
        vacancy_id = self._store.vacancy_ids.get((channel, external_id))
        return self._store.vacancies.get(vacancy_id) if vacancy_id is not None else None

    async def delete(self, vacancy_id: UUID) -> bool:
        vacancy = self._store.vacancies.pop(vacancy_id, None)
        if vacancy is None:
            return False
        self._store.vacancy_ids.pop((vacancy.channel, vacancy.external_id), None)
        return True

    async def list_active(self, company_id: UUID) -> Sequence[Vacancy]:
        return [
            vacancy
            for vacancy in self._store.vacancies.values()
            if vacancy.company_id == company_id and vacancy.is_active
        ]


class InMemoryCandidateRepository:
    """Репозиторий кандидатов на словарях."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def upsert(self, candidate: Candidate) -> Candidate:
        key = (candidate.channel, candidate.external_id)
        existing_id = self._store.candidate_ids.get(key)
        if existing_id is not None and existing_id != candidate.id:
            candidate = replace(candidate, id=existing_id)
        self._store.candidates[candidate.id] = candidate
        self._store.candidate_ids[key] = candidate.id
        return candidate

    async def get(self, candidate_id: UUID) -> Candidate | None:
        return self._store.candidates.get(candidate_id)

    async def get_by_external(self, channel: Channel, external_id: str) -> Candidate | None:
        candidate_id = self._store.candidate_ids.get((channel, external_id))
        return self._store.candidates.get(candidate_id) if candidate_id is not None else None


class InMemoryResumeRepository:
    """Репозиторий резюме на словарях."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def save(self, resume: Resume) -> Resume:
        self._store.resumes[resume.id] = resume
        bucket = self._store.resumes_by_candidate.setdefault(resume.candidate_id, [])
        if resume.id not in bucket:
            bucket.append(resume.id)
        return resume

    async def get(self, resume_id: UUID) -> Resume | None:
        return self._store.resumes.get(resume_id)

    async def get_latest_for_candidate(self, candidate_id: UUID) -> Resume | None:
        ids = self._store.resumes_by_candidate.get(candidate_id, [])
        candidates = [self._store.resumes[item] for item in ids if item in self._store.resumes]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.fetched_at)


class InMemoryConversationRepository:
    """Репозиторий диалогов на словарях."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store
        self.locked_ids: set[UUID] = set()

    async def upsert(self, conversation: Conversation) -> Conversation:
        key = (conversation.channel, conversation.external_id)
        existing_id = self._store.conversation_ids.get(key)
        if existing_id is not None and existing_id != conversation.id:
            conversation = replace(conversation, id=existing_id)
        self._store.conversations[conversation.id] = conversation
        self._store.conversation_ids[key] = conversation.id
        return conversation

    async def get(self, conversation_id: UUID) -> Conversation | None:
        return self._store.conversations.get(conversation_id)

    async def get_by_external(self, channel: Channel, external_id: str) -> Conversation | None:
        conversation_id = self._store.conversation_ids.get((channel, external_id))
        return self._store.conversations.get(conversation_id) if conversation_id is not None else None

    async def get_with_lock(self, conversation_id: UUID) -> Conversation | None:
        """Прочитать диалог с «блокировкой».

        В памяти блокировка не нужна (тесты однопоточные), но факт захвата
        отмечается: тест может убедиться, что сервис читает диалог именно так.
        Настоящий ``SELECT ... FOR UPDATE`` живёт в инфраструктурном репозитории.
        """
        conversation = self._store.conversations.get(conversation_id)
        if conversation is not None:
            self.locked_ids.add(conversation_id)
        return conversation

    async def count_for_vacancy(self, vacancy_id: UUID) -> int:
        return sum(
            1 for conversation in self._store.conversations.values() if conversation.vacancy_id == vacancy_id
        )

    async def list_by_state(
        self, states: Sequence[ConversationState], *, limit: int = 100
    ) -> Sequence[Conversation]:
        wanted = set(states)
        result = [
            conversation
            for conversation in self._store.conversations.values()
            if conversation.state in wanted
        ]
        return result[:limit]


class InMemoryMessageRepository:
    """Репозиторий сообщений на словарях."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def add(self, message: Message) -> Message:
        self._store.messages_by_conversation.setdefault(message.conversation_id, []).append(message.id)
        if message.external_id:
            key = (message.conversation_id, message.external_id)
            existing_id = self._store.message_external_ids.get(key)
            if existing_id is not None:
                # Повторная запись того же внешнего сообщения — не ошибка:
                # возвращаем уже сохранённое, дубль не создаём.
                return self._store.messages[existing_id]
            self._store.message_external_ids[key] = message.id
        self._store.messages[message.id] = message
        return message

    async def list_for_conversation(self, conversation_id: UUID, *, limit: int = 50) -> Sequence[Message]:
        ids = self._store.messages_by_conversation.get(conversation_id, [])
        messages = [
            self._store.messages[message_id] for message_id in ids if message_id in self._store.messages
        ]
        messages.sort(key=lambda message: message.created_at)
        return messages[-limit:]

    async def exists_external_id(self, conversation_id: UUID, external_id: str) -> bool:
        return (conversation_id, external_id) in self._store.message_external_ids


class InMemoryQuestionAnswerRepository:
    """Ответы кандидатов на словаре."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def add(self, answer: QuestionAnswer) -> QuestionAnswer:
        self._store.answers[answer.id] = answer
        return answer

    async def list_for_conversation(self, conversation_id: UUID) -> Sequence[QuestionAnswer]:
        items = [item for item in self._store.answers.values() if item.conversation_id == conversation_id]
        items.sort(key=lambda item: item.created_at)
        return items

    async def latest_for_criterion(self, conversation_id: UUID, criterion_key: str) -> QuestionAnswer | None:
        items = [
            item
            for item in self._store.answers.values()
            if item.conversation_id == conversation_id and item.criterion_key == criterion_key
        ]
        return max(items, key=lambda item: item.created_at) if items else None


class InMemoryPaidActionRepository:
    """Журнал списаний на словаре: сумма за период и запись."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def add(self, action: PaidAction) -> PaidAction:
        self._store.paid_actions[action.id] = action
        return action

    async def spent_since(self, company_id: UUID, since: datetime) -> float:
        return float(
            sum(
                item.cost
                for item in self._store.paid_actions.values()
                if item.company_id == company_id and item.created_at is not None and item.created_at >= since
            )
        )


class InMemoryScoringProfileRepository:
    """Профили скоринга на словарях: версии, публикация, откат."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def add(self, profile: ScoringProfile) -> ScoringProfile:
        self._store.profiles[profile.id] = profile
        return profile

    async def get(self, profile_id: UUID) -> ScoringProfile | None:
        return self._store.profiles.get(profile_id)

    async def get_live(self, vacancy_id: UUID) -> ScoringProfile | None:
        for profile in self._store.profiles.values():
            if profile.vacancy_id == vacancy_id and profile.is_live:
                return profile
        return None

    async def get_latest_version(self, vacancy_id: UUID) -> ScoringProfile | None:
        versions = [profile for profile in self._store.profiles.values() if profile.vacancy_id == vacancy_id]
        return max(versions, key=lambda item: item.version) if versions else None

    async def list_versions(self, vacancy_id: UUID, *, limit: int = 50) -> Sequence[ScoringProfile]:
        versions = sorted(
            (profile for profile in self._store.profiles.values() if profile.vacancy_id == vacancy_id),
            key=lambda item: item.version,
            reverse=True,
        )
        return versions[:limit]

    async def save(self, profile: ScoringProfile) -> ScoringProfile:
        self._store.profiles[profile.id] = profile
        return profile

    async def publish(self, profile_id: UUID) -> ScoringProfile:
        profile = self._store.profiles.get(profile_id)
        if profile is None:
            raise NotFound(f"Профиль {profile_id} не найден")
        for other in self._store.profiles.values():
            if other.vacancy_id == profile.vacancy_id and other.is_live and other.id != profile.id:
                other.status = ProfileStatus.ARCHIVED
        profile.status = ProfileStatus.ACTIVE
        profile.published_at = datetime.now(tz=UTC)
        return profile

    async def activate_version(self, vacancy_id: UUID, version: int) -> ScoringProfile:
        target = next(
            (
                profile
                for profile in self._store.profiles.values()
                if profile.vacancy_id == vacancy_id and profile.version == version
            ),
            None,
        )
        if target is None:
            raise NotFound(f"Версия {version} профиля вакансии {vacancy_id} не найдена")
        return await self.publish(target.id)


class InMemoryMessageDraftRepository:
    """Репозиторий черновиков на словарях (очередь подтверждения)."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def add(self, draft: DraftMessage) -> DraftMessage:
        # Вытесняем прежний ожидающий черновик — как в SQL-реализации.
        for existing in self._store.drafts.values():
            if (
                existing.conversation_id == draft.conversation_id
                and existing.status is DraftStatus.PENDING
                and existing.id != draft.id
            ):
                existing.status = DraftStatus.SUPERSEDED
        self._store.drafts[draft.id] = draft
        return draft

    async def get(self, draft_id: UUID) -> DraftMessage | None:
        return self._store.drafts.get(draft_id)

    async def get_pending_for_conversation(self, conversation_id: UUID) -> DraftMessage | None:
        for draft in self._store.drafts.values():
            if draft.conversation_id == conversation_id and draft.status is DraftStatus.PENDING:
                return draft
        return None

    async def list_pending(self, *, limit: int = 100) -> Sequence[DraftMessage]:
        pending = [draft for draft in self._store.drafts.values() if draft.status is DraftStatus.PENDING]
        pending.sort(key=lambda item: item.created_at)
        return pending[:limit]

    async def save(self, draft: DraftMessage) -> DraftMessage:
        self._store.drafts[draft.id] = draft
        return draft


class InMemoryPrivacyRepository:
    """Удаление и выгрузка ПДн на словарях — для тестов и демо."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def purge_before(
        self,
        *,
        resumes_before: datetime,
        messages_before: datetime,
        screenings_before: datetime,
        drafts_before: datetime,
        audit_before: datetime,
        batch_size: int = 1000,
    ) -> PurgeStats:
        removed_resumes = [
            key for key, resume in self._store.resumes.items() if resume.fetched_at < resumes_before
        ][:batch_size]
        for key in removed_resumes:
            del self._store.resumes[key]

        removed_messages = [
            key for key, message in self._store.messages.items() if message.created_at < messages_before
        ][:batch_size]
        for key in removed_messages:
            del self._store.messages[key]

        removed_screenings = [
            key for key, result in self._store.screenings.items() if result.created_at < screenings_before
        ][:batch_size]
        for key in removed_screenings:
            del self._store.screenings[key]

        removed_drafts = [
            key for key, draft in self._store.drafts.items() if draft.created_at < drafts_before
        ][:batch_size]
        for key in removed_drafts:
            del self._store.drafts[key]

        # Журнала в тестовом хранилище нет: возвращаем 0, не притворяясь, что удалили.
        # ``audit_before`` принимается по контракту порта и здесь не используется.
        del audit_before
        return PurgeStats(
            resumes=len(removed_resumes),
            messages=len(removed_messages),
            screenings=len(removed_screenings),
            drafts=len(removed_drafts),
            audit=0,
        )

    async def export_candidate(self, candidate_id: UUID) -> dict[str, Any] | None:
        candidate = self._store.candidates.get(candidate_id)
        if candidate is None:
            return None
        conversations = [
            conversation
            for conversation in self._store.conversations.values()
            if conversation.candidate_id == candidate_id
        ]
        conversation_ids = {item.id for item in conversations}
        return {
            "candidate": {
                "id": str(candidate.id),
                "full_name": candidate.full_name,
                "phone": candidate.phone,
                "email": candidate.email,
                "city": candidate.city,
                "channel": candidate.channel.value,
            },
            "resumes": [
                {"id": str(item.id), "text": item.raw_text}
                for item in self._store.resumes.values()
                if item.candidate_id == candidate_id
            ],
            "conversations": [{"id": str(item.id), "state": item.state.value} for item in conversations],
            "messages": [
                {"body": item.body}
                for item in self._store.messages.values()
                if item.conversation_id in conversation_ids
            ],
            "screenings": [
                {"total_score": item.total_score, "verdict": item.verdict.value}
                for item in self._store.screenings.values()
                if item.candidate_id == candidate_id
            ],
            "bookings": [
                {"id": str(item.id), "status": item.status.value}
                for item in self._store.bookings.values()
                if item.candidate_id == candidate_id
            ],
        }

    async def erase_candidate(self, candidate_id: UUID) -> bool:
        candidate = self._store.candidates.get(candidate_id)
        if candidate is None:
            return False
        conversation_ids = {
            conversation.id
            for conversation in self._store.conversations.values()
            if conversation.candidate_id == candidate_id
        }
        for key in [
            key for key, item in self._store.messages.items() if item.conversation_id in conversation_ids
        ]:
            del self._store.messages[key]
        for key in [
            key for key, item in self._store.drafts.items() if item.conversation_id in conversation_ids
        ]:
            del self._store.drafts[key]
        for key in [key for key, item in self._store.resumes.items() if item.candidate_id == candidate_id]:
            del self._store.resumes[key]
        for key in [key for key, item in self._store.screenings.items() if item.candidate_id == candidate_id]:
            del self._store.screenings[key]

        candidate.full_name = "Кандидат (данные удалены)"
        candidate.phone = None
        candidate.email = None
        return True


class InMemoryScreeningRepository:
    """Репозиторий скринингов на словарях.

    ``content_hash`` приходит из сервиса: только он знает версию профиля, по
    которой посчитан результат. Если поле не заполнено, хэш выводится из резюме
    без привязки к профилю — так ведёт себя и инфраструктурная реализация.
    """

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def save(self, result: ScreeningResult) -> ScreeningResult:
        self._store.screenings[result.id] = result
        bucket = self._store.screenings_by_candidate.setdefault((result.candidate_id, result.vacancy_id), [])
        if result.id not in bucket:
            bucket.append(result.id)
        content_hash = result.content_hash
        if content_hash is None:
            resume = self._store.resumes.get(result.resume_id)
            if resume is not None:
                content_hash = Resume.content_hash(resume.raw_text, vacancy_id=result.vacancy_id)
        if content_hash:
            ids = self._store.screening_hashes.setdefault(content_hash, [])
            if result.id not in ids:
                ids.append(result.id)
        return result

    async def get_latest(self, candidate_id: UUID, vacancy_id: UUID) -> ScreeningResult | None:
        ids = self._store.screenings_by_candidate.get((candidate_id, vacancy_id), [])
        candidates = [self._store.screenings[item] for item in ids if item in self._store.screenings]
        if not candidates:
            return None
        return max(candidates, key=lambda item: item.created_at)

    async def get_by_content_hash(self, content_hash: str) -> ScreeningResult | None:
        ids = self._store.screening_hashes.get(content_hash)
        if not ids:
            return None
        latest: ScreeningResult | None = None
        for screening_id in ids:
            result = self._store.screenings.get(screening_id)
            if result is None:
                continue
            if latest is None or result.created_at >= latest.created_at:
                latest = result
        return latest


class InMemoryBookingRepository:
    """Репозиторий броней на словарях.

    Повторяет ограничения боевой схемы: уникальность ``idempotency_key`` и
    частичный уникальный индекс ``bookings(manager_id, starts_at)`` для активных
    броней. Второе — последний рубеж против двойной брони: без него тестовый
    контур не воспроизводил бы гонку двух кандидатов на одно время.
    """

    #: Статусы, которые занимают время (совпадает с частичным индексом БД).
    ACTIVE_STATUSES: frozenset[BookingStatus] = frozenset({BookingStatus.PROPOSED, BookingStatus.CONFIRMED})

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def save(self, booking: Booking) -> Booking:
        key = booking.idempotency_key
        if key:
            existing_id = self._store.booking_keys.get(key)
            if existing_id is not None and existing_id != booking.id:
                # Уникальный индекс по ключу идемпотентности: вторая бронь на
                # тот же слот не создаётся.
                return self._store.bookings[existing_id]
        if booking.status in self.ACTIVE_STATUSES:
            clash = next(
                (
                    item
                    for item in self._store.bookings.values()
                    if item.id != booking.id
                    and item.manager_id == booking.manager_id
                    and item.start == booking.start
                    and item.status in self.ACTIVE_STATUSES
                ),
                None,
            )
            if clash is not None:
                msg = (
                    f"Слот {booking.start.isoformat()} у менеджера {booking.manager_id} "
                    f"уже занят бронью {clash.id}: двойная бронь недопустима"
                )
                raise SlotConflict(msg)
        if key:
            self._store.booking_keys[key] = booking.id
        self._store.bookings[booking.id] = booking
        return booking

    async def get(self, booking_id: UUID) -> Booking | None:
        return self._store.bookings.get(booking_id)

    async def get_by_idempotency_key(self, key: str) -> Booking | None:
        booking_id = self._store.booking_keys.get(key)
        return self._store.bookings.get(booking_id) if booking_id is not None else None

    async def get_for_conversation(self, conversation_id: UUID) -> Booking | None:
        matches = [
            booking for booking in self._store.bookings.values() if booking.conversation_id == conversation_id
        ]
        if not matches:
            return None
        return max(matches, key=lambda booking: booking.created_at)

    async def list_active_for_manager(
        self, manager_id: str, *, start: datetime, end: datetime
    ) -> Sequence[Booking]:
        active = {BookingStatus.PROPOSED, BookingStatus.CONFIRMED}
        return [
            booking
            for booking in self._store.bookings.values()
            if booking.manager_id == manager_id
            and booking.status in active
            and booking.start < end
            and start < booking.end
        ]

    async def find_active_overlap(self, manager_id: str, *, start: datetime, end: datetime) -> Booking | None:
        """Первая активная бронь менеджера, пересекающаяся с ``[start, end)``."""
        matches = await self.list_active_for_manager(manager_id, start=start, end=end)
        return matches[0] if matches else None


class InMemoryOutboxRepository:
    """Репозиторий outbox на словарях.

    Пауза между повторами (backoff) — свойство **хранилища**, а не только
    отложенной задачи очереди: ``mark`` отодвигает ``available_at``, а
    ``claim_batch`` берёт лишь то, что уже доступно. Иначе любой следующий
    ``drain`` обходил бы экспоненциальную паузу.
    """

    def __init__(self, store: InMemoryStore, *, clock: Any = None) -> None:
        self._store = store
        self._clock = clock

    async def add(self, entry: OutboxEntry) -> OutboxEntry:
        existing_id = self._store.outbox_keys.get(entry.dedupe_key)
        if existing_id is not None:
            # UNIQUE(dedupe_key): повторная постановка того же шага ничего не
            # добавляет — именно это защищает от двойной обработки.
            return self._store.outbox[existing_id]
        self._store.outbox[entry.id] = entry
        self._store.outbox_keys[entry.dedupe_key] = entry.id
        return entry

    async def claim_batch(self, *, limit: int, now: datetime) -> Sequence[OutboxEntry]:
        claimed: list[OutboxEntry] = []
        for entry in self._store.outbox.values():
            if len(claimed) >= limit:
                break
            if entry.id in self._store.claimed_outbox:
                continue
            if entry.status not in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
                continue
            if entry.available_at > now:
                continue
            # Счётчик попыток увеличивается при захвате — так же ведёт себя
            # реальная реализация (``UPDATE ... SET attempts = attempts + 1``).
            entry.attempts += 1
            self._store.claimed_outbox.add(entry.id)
            claimed.append(entry)
        return claimed

    async def mark(self, entry_id: UUID, status: OutboxStatus, *, error: str | None = None) -> None:
        entry = self._store.outbox.get(entry_id)
        if entry is None:
            return
        entry.status = status
        entry.last_error = error
        # Захват снимаем: запись снова доступна (при FAILED — для повтора).
        self._store.claimed_outbox.discard(entry_id)
        if status in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            # Та же формула, что и в SQL-реализации: 30 c * 2^(attempts-1), потолок 1 час.
            entry.available_at = self._now() + timedelta(seconds=backoff_delay_s(entry.attempts))

    def _now(self) -> datetime:
        """Текущее время хранилища (часы контейнера или системное)."""
        if self._clock is not None:
            return cast(datetime, self._clock.now())
        return datetime.now(tz=UTC)


class InMemoryIdempotencyRepository:
    """Ключи идемпотентности на словарях."""

    def __init__(self, store: InMemoryStore) -> None:
        self._store = store

    async def acquire(self, key: str, *, scope: str, ttl_hours: int = 72) -> bool:
        marker = (scope, key)
        if marker in self._store.idempotency:
            return False
        self._store.idempotency[marker] = f"{ttl_hours}h"
        return True


class InMemoryUnitOfWork:
    """Полноценный ``UnitOfWork`` на словарях.

    Транзакция — снимок состояния: ``commit`` его отбрасывает, ``rollback``
    (и любой выход по исключению) восстанавливает данные. Ключи идемпотентности
    живут в том же хранилище, поэтому откат освобождает и их.
    """

    def __init__(self, store: InMemoryStore, *, clock: Any = None) -> None:
        self.store = store
        self.clock = clock
        self.companies = InMemoryCompanyRepository(store)
        self.profiles = InMemoryScoringProfileRepository(store)
        self.paid_actions = InMemoryPaidActionRepository(store)
        self.answers = InMemoryQuestionAnswerRepository(store)
        self.vacancies = InMemoryVacancyRepository(store)
        self.candidates = InMemoryCandidateRepository(store)
        self.resumes = InMemoryResumeRepository(store)
        self.conversations = InMemoryConversationRepository(store)
        self.messages = InMemoryMessageRepository(store)
        self.drafts = InMemoryMessageDraftRepository(store)
        self.screenings = InMemoryScreeningRepository(store)
        self.bookings = InMemoryBookingRepository(store)
        self.outbox = InMemoryOutboxRepository(store, clock=clock)
        self.idempotency = InMemoryIdempotencyRepository(store)
        self.privacy = InMemoryPrivacyRepository(store)
        self._snapshot: InMemoryStore | None = None
        self._committed = False

    async def __aenter__(self) -> Self:
        self._snapshot = copy.deepcopy(self.store)
        self._committed = False
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: Any,
    ) -> None:
        if exc_type is not None or not self._committed:
            self._rollback_if_needed()
        self._snapshot = None

    async def commit(self) -> None:
        self._committed = True

    async def rollback(self) -> None:
        self._rollback_if_needed()
        self._committed = False

    def _rollback_if_needed(self) -> None:
        if self._snapshot is not None:
            _restore_store(self.store, self._snapshot)


# --------------------------------------------------------------------------- #
# Fake-канал
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class SentMessageRecord:
    """Запись об отправленном сообщении — для проверок в тестах."""

    conversation_external_id: str
    text: str
    idempotency_key: str
    kind_hint: str | None
    external_id: str


#: Действия, которые fake-площадка разрешает «из коробки» — те же ключи, что
#: боевой адаптер hh.ru получает в ``actions[]`` ответа ``GET /negotiations/{id}``.
#: Неизвестное действие обязано падать ``NotFound``: молчаливое «ок» скрыло бы
#: расхождение с площадкой (см. ``HhChannelAdapter.perform_action``).
FAKE_CHANNEL_ACTIONS: dict[str, ChannelAction] = {
    "invitation": ChannelAction(
        key="invitation",
        title="Пригласить",
        url="",
        method="PUT",
        requires_message=True,
        extra_args={"arguments": {"message": True}},
    ),
    "discard": ChannelAction(
        key="discard",
        title="Отказать",
        url="",
        method="PUT",
        requires_message=True,
        extra_args={"arguments": {"message": True}},
    ),
    "rejection": ChannelAction(key="rejection", title="Отказ", url="", method="PUT"),
    "archive": ChannelAction(key="archive", title="В архив", url="", method="PUT"),
    "consider": ChannelAction(key="consider", title="Вернуть в работу", url="", method="PUT"),
}


class FakeChannelAdapter:
    """Площадка в памяти: вакансии, отклики, чаты, действия.

    ``send_message`` идемпотентен по ``(conversation_external_id, idempotency_key)``
    — ровно как требует порт: повтор с тем же ключом возвращает прежний
    результат и не создаёт второе сообщение.

    ``is_fake`` — признак демо-адаптера: он не обращается к площадке и не
    списывает деньги. Бюджет платных действий опирается на этот флаг, иначе
    тесты и демо упирались бы в лимиты, которых в реальности нет.
    """

    #: Демо-адаптер не тратит платную квоту площадки.
    is_fake: bool = True

    channel: Channel

    def __init__(self, channel: Channel = Channel.HH, *, clock: Any = None) -> None:
        self.channel = channel
        self._clock = clock
        self._vacancies: dict[str, VacancyPayload] = {}
        self._responses: list[InboundResponse] = []
        self._delivered: set[str] = set()
        self._resumes: dict[str, ResumePayload] = {}
        self._denied_resumes: set[str] = set()
        self._chats: dict[str, list[InboundMessage]] = {}
        self._sent_by_key: dict[tuple[str, str], SentMessage] = {}
        self._actions: dict[str, dict[str, ChannelAction]] = {}
        self.sent: list[SentMessageRecord] = []
        self.actions: list[tuple[str, str]] = []
        self.send_error: Exception | None = None
        self.fetch_error: Exception | None = None

    # ---- подготовка данных (используется тестами и демо) ---- #

    def add_vacancy(self, payload: VacancyPayload) -> None:
        self._vacancies[payload.external_id] = payload

    def add_response(self, response: InboundResponse) -> None:
        self._responses.append(response)

    def add_resume(self, payload: ResumePayload) -> None:
        self._resumes[payload.external_id] = payload

    def deny_resume(self, resume_external_id: str) -> None:
        """Имитировать отказ в доступе к резюме (нет прав или квота)."""
        self._denied_resumes.add(resume_external_id)

    def add_inbound_message(self, conversation_external_id: str, message: InboundMessage) -> None:
        self._chats.setdefault(conversation_external_id, []).append(message)

    def add_action(self, conversation_external_id: str, action: ChannelAction) -> None:
        """Разрешить дополнительное действие по диалогу (подготовка данных)."""
        self._actions.setdefault(conversation_external_id, {})[action.key] = action

    def available_actions(self, conversation_external_id: str) -> tuple[ChannelAction, ...]:
        """Действия, которые «площадка» разрешает по этому диалогу.

        Как и у боевого адаптера, состав действий приходит из ``get_conversation``:
        по умолчанию — стандартный набор переговоров hh.ru.
        """
        registered = self._actions.get(conversation_external_id)
        if registered:
            return tuple(registered.values())
        return tuple(FAKE_CHANNEL_ACTIONS.values())

    def texts_for(self, conversation_external_id: str) -> list[str]:
        return [
            record.text for record in self.sent if record.conversation_external_id == conversation_external_id
        ]

    # ---- ChannelPort ---- #

    async def healthcheck(self) -> bool:
        return True

    async def list_vacancies(self) -> Sequence[VacancyPayload]:
        return list(self._vacancies.values())

    async def fetch_new_responses(
        self, *, vacancy_external_id: str | None = None, limit: int = 50
    ) -> Sequence[InboundResponse]:
        if self.fetch_error is not None:
            raise self.fetch_error
        fresh = [
            response
            for response in self._responses
            if response.conversation_external_id not in self._delivered
            and (vacancy_external_id is None or response.vacancy_external_id == vacancy_external_id)
        ][:limit]
        for response in fresh:
            self._delivered.add(response.conversation_external_id)
        return fresh

    async def fetch_resume(self, resume_external_id: str) -> ResumePayload:
        if resume_external_id in self._denied_resumes:
            msg = f"Нет доступа к резюме {resume_external_id} (fake: доступ запрещён)"
            raise ResumeAccessDenied(msg)
        payload = self._resumes.get(resume_external_id)
        if payload is None:
            msg = f"Резюме {resume_external_id} недоступно в fake-канале"
            raise ResumeAccessDenied(msg)
        return payload

    async def get_conversation(self, conversation_external_id: str) -> ConversationSnapshot:
        return ConversationSnapshot(
            external_id=conversation_external_id,
            actions=self.available_actions(conversation_external_id),
            unread_count=0,
            messaging_available=True,
        )

    async def list_messages(
        self, conversation_external_id: str, *, since: datetime | None = None
    ) -> Sequence[InboundMessage]:
        messages = self._chats.get(conversation_external_id, [])
        if since is None:
            return list(messages)
        return [message for message in messages if message.created_at >= since]

    async def send_message(
        self,
        conversation_external_id: str,
        text: str,
        *,
        idempotency_key: str,
        kind_hint: str | None = None,
    ) -> SentMessage:
        if self.send_error is not None:
            raise self.send_error
        key = (conversation_external_id, idempotency_key)
        cached = self._sent_by_key.get(key)
        if cached is not None:
            return cached
        external_id = f"{self.channel.value}-msg-{len(self.sent) + 1}"
        sent = SentMessage(external_id=external_id, sent_at=self._now())
        self._sent_by_key[key] = sent
        self.sent.append(
            SentMessageRecord(
                conversation_external_id=conversation_external_id,
                text=text,
                idempotency_key=idempotency_key,
                kind_hint=kind_hint,
                external_id=external_id,
            )
        )
        self._chats.setdefault(conversation_external_id, []).append(
            InboundMessage(
                external_id=external_id,
                direction=MessageDirection.OUTBOUND,
                text=text,
                created_at=sent.sent_at,
            )
        )
        return sent

    async def perform_action(
        self,
        conversation_external_id: str,
        action_key: str,
        *,
        message: str | None = None,
        idempotency_key: str,
        extra_args: dict[str, str] | None = None,
    ) -> None:
        """Выполнить действие, разрешённое площадкой по этому диалогу.

        Неизвестное действие — доменная ошибка ``NotFound``, как у боевого
        адаптера hh.ru (``_find_action``): молчаливое «ок» означало бы, что
        тестовый контур не проверяет гарантию «площадка не разрешает — не
        выполняем».
        """
        allowed = self.available_actions(conversation_external_id)
        action = next((item for item in allowed if item.key == action_key), None)
        if action is None:
            available = ", ".join(sorted(item.key for item in allowed)) or "нет"
            msg = (
                f"{self.channel.value} не разрешает действие {action_key!r} для отклика "
                f"{conversation_external_id}. Доступные действия: {available}"
            )
            raise NotFound(msg)
        self.actions.append((conversation_external_id, action_key))
        logger.debug(
            "fake_channel_action",
            channel=self.channel.value,
            action=action_key,
            message=message,
            idempotency_key=idempotency_key,
            extra_args=extra_args,
        )

    def _now(self) -> datetime:
        if self._clock is not None:
            return cast(datetime, self._clock.now())
        return datetime.now(tz=UTC)


# --------------------------------------------------------------------------- #
# Fake-календарь
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class FakeMeeting:
    """Встреча в fake-календаре (эквивалент ответа Talantix)."""

    external_id: str
    title: str
    start: datetime
    end: datetime
    timezone: str
    manager_id: str
    description: str = ""
    idempotency_key: str = ""


class FakeCalendarAdapter:
    """Календарь в памяти: задаваемая занятость, рабочие часы, встречи.

    Умеет имитировать сбои: ``create_error`` (таймаут на создании),
    ``get_error`` (таймаут на перечитывании) и ``hide_meetings`` (встреча
    «создана», но не читается) — это два разных риска из ARCHITECTURE.md, п. 6.3.
    """

    def __init__(
        self,
        *,
        clock: Any = None,
        manager_ids: Sequence[str] = ("manager-1",),
        timezone: str = "Europe/Moscow",
        work_start_hour: int = 10,
        work_end_hour: int = 18,
    ) -> None:
        self._clock = clock
        self.manager_ids = list(manager_ids)
        self.timezone = timezone
        self.work_start_hour = work_start_hour
        self.work_end_hour = work_end_hour
        self.busy: list[BusyInterval] = []
        self.meetings: dict[str, FakeMeeting] = {}
        self.find_calls: list[dict[str, Any]] = []
        self.create_calls: list[dict[str, Any]] = []
        self.create_error: Exception | None = None
        self.get_error: Exception | None = None
        self.hide_meetings = False
        self._by_idempotency_key: dict[str, str] = {}

    def add_busy(self, interval: BusyInterval) -> None:
        self.busy.append(interval)

    def add_busy_range(self, start: datetime, end: datetime) -> None:
        self.busy.append(BusyInterval(start=start, end=end))

    def _now(self) -> datetime:
        """Текущее «сейчас» календаря: часы контейнера или системное время."""
        if self._clock is not None:
            return cast(datetime, self._clock.now())
        return datetime.now(tz=UTC)

    # ---- CalendarPort ---- #

    def _meeting_intervals(self) -> list[BusyInterval]:
        """Созданные встречи как занятость.

        Так же ведёт себя боевой Talantix: ``calendarMeetings`` отдаёт встречи,
        и созданная нами встреча обязана быть видна как занятость — иначе двойник
        не воспроизводит повторное предложение уже занятого времени.
        """
        return [
            BusyInterval(
                start=meeting.start,
                end=meeting.end,
                manager_id=meeting.manager_id,
                title=meeting.title,
            )
            for meeting in self.meetings.values()
        ]

    async def list_busy(
        self, *, manager_ids: Sequence[str], start: datetime, end: datetime
    ) -> Sequence[BusyInterval]:
        managers = set(manager_ids) or set(self.manager_ids)
        intervals = [*self.busy, *self._meeting_intervals()]
        return [
            interval
            for interval in intervals
            if interval.start < end
            and start < interval.end
            and (interval.manager_id is None or interval.manager_id in managers)
        ]

    async def find_free_slots(
        self,
        *,
        manager_ids: Sequence[str],
        start: datetime,
        end: datetime,
        duration_minutes: int,
        limit: int = 3,
    ) -> Sequence[InterviewSlot]:
        """Детерминированная сетка: рабочие часы по будням, шаг — длительность.

        Сетка привязана к календарным дням, а не к моменту вызова: один и тот
        же диалог получает один и тот же список слотов, пока не сменились сутки.
        Прошедшее время не предлагается: кандидат не может выбрать «вчера»
        (боевой адаптер Talantix отсекает такие слоты точно так же).
        """
        managers = list(manager_ids) or list(self.manager_ids)
        manager_id = managers[0]
        now = self._now()
        self.find_calls.append(
            {
                "manager_ids": managers,
                "start": start,
                "end": end,
                "duration_minutes": duration_minutes,
                "limit": limit,
            }
        )
        busy = await self.list_busy(manager_ids=managers, start=start, end=end)
        slots: list[InterviewSlot] = []
        day = start.date()
        last_day = end.date()
        while day <= last_day and len(slots) < limit:
            if day.weekday() < 5:  # будни
                for hour in range(self.work_start_hour, self.work_end_hour):
                    if len(slots) >= limit:
                        break
                    slot_start = datetime(day.year, day.month, day.day, hour, tzinfo=UTC)
                    if slot_start < start:
                        continue
                    slot_end = slot_start + timedelta(minutes=duration_minutes)
                    if slot_end > end:
                        continue
                    if slot_start <= now:
                        # Слот уже начался: предлагать его нельзя.
                        continue
                    if any(slot_start < interval.end and interval.start < slot_end for interval in busy):
                        continue
                    slots.append(
                        InterviewSlot(
                            start=slot_start,
                            end=slot_end,
                            manager_id=manager_id,
                            timezone=self.timezone,
                        )
                    )
            day += timedelta(days=1)
        return slots

    async def create_meeting(
        self,
        *,
        title: str,
        start: datetime,
        end: datetime,
        timezone: str,
        manager_ids: Sequence[str],
        candidate_email: str | None = None,
        description: str = "",
        idempotency_key: str = "",
    ) -> object:
        self.create_calls.append(
            {
                "title": title,
                "start": start,
                "end": end,
                "timezone": timezone,
                "manager_ids": list(manager_ids),
                "candidate_email": candidate_email,
                "description": description,
                "idempotency_key": idempotency_key,
            }
        )
        if self.create_error is not None:
            raise self.create_error
        if idempotency_key:
            existing_id = self._by_idempotency_key.get(idempotency_key)
            if existing_id is not None:
                return self.meetings[existing_id]
        external_id = f"talantix-meeting-{len(self.meetings) + 1}"
        meeting = FakeMeeting(
            external_id=external_id,
            title=title,
            start=start,
            end=end,
            timezone=timezone,
            manager_id=manager_ids[0] if manager_ids else self.manager_ids[0],
            description=description,
            idempotency_key=idempotency_key,
        )
        self.meetings[external_id] = meeting
        if idempotency_key:
            self._by_idempotency_key[idempotency_key] = external_id
        return meeting

    async def cancel_meeting(self, external_id: str) -> None:
        self.meetings.pop(external_id, None)

    async def get_meeting(self, external_id: str) -> object | None:
        if self.get_error is not None:
            raise self.get_error
        if self.hide_meetings:
            return None
        return self.meetings.get(external_id)


# --------------------------------------------------------------------------- #
# Fake-LLM
# --------------------------------------------------------------------------- #

#: Слова, которые встречаются почти в любом резюме и потому не различают
#: кандидатов: по ним нельзя «зачесть» критерий.
_CRITERION_STOPWORDS = frozenset(
    {
        "опыт",
        "опыта",
        "работа",
        "работы",
        "работе",
        "разработка",
        "разработки",
        "навык",
        "навыки",
        "знание",
        "знания",
        "умение",
        "уровень",
        "владение",
        "требование",
        "требования",
        "обязательно",
        "желательно",
        "будет",
        "плюс",
        "или",
        "для",
        "при",
        "как",
        "что",
        "это",
        "and",
        "the",
        "with",
        "for",
        "not",
        "years",
        "year",
    }
)

_TOKEN_RE = re.compile(r"[^0-9a-zа-я]+")
_YEARS_RE = re.compile(r"\b(\d{1,2})\s*(?:год|года|лет|года|years?|y\.)")
_YEARS_PLUS_RE = re.compile(r"\b(\d{1,2})\s*\+")
_PERIOD_RE = re.compile(r"\b(19\d{2}|20\d{2})\s*(?:-|–|—|по|to)\s*(19\d{2}|20\d{2})\b")
_REQUIRED_YEARS_RE = re.compile(r"(?:от|не менее|минимум|более)\s*(\d{1,2})|(\d{1,2})\s*\+")

#: Ниже этой оценки критерий считается пробелом.
_FAKE_GAP_THRESHOLD = 50.0

#: Оценка по критерию, для которого не нашлось подтверждения в тексте.
_FAKE_NO_EVIDENCE_SCORE = 15.0


def _tokens(text: str) -> list[str]:
    return [token for token in _TOKEN_RE.split(normalize_text(text or "")) if token]


def _significant_tokens(text: str) -> list[str]:
    result: list[str] = []
    for token in _tokens(text):
        if len(token) < 3 or token.isdigit() or token in _CRITERION_STOPWORDS:
            continue
        if token not in result:
            result.append(token)
    return result


def _stem(token: str) -> str:
    """Усечённая основа: грубо, но снимает русские падежи и английские формы."""
    return token[:5] if len(token) > 5 else token


def _text_has_token(text_tokens: set[str], token: str) -> bool:
    stem = _stem(token)
    return any(candidate.startswith(stem) for candidate in text_tokens)


def _find_evidence(text: str, token: str) -> str:
    """Первая строка резюме, где встречается слово (цитата-обоснование)."""
    stem = _stem(token)
    for line in text.splitlines():
        normalized = normalize_text(line)
        if any(candidate.startswith(stem) for candidate in _TOKEN_RE.split(normalized)):
            return line.strip()[:300]
    return ""


def extract_years(text: str) -> int:
    """Годы опыта из текста резюме.

    Считаем по двум признакам: прямой формулировке («5 лет») и периодам работы
    («с 2019 по 2024»). Именно так это делает человек, читая резюме.
    """
    normalized = normalize_text(text or "")
    candidates: list[int] = []
    candidates.extend(int(value) for value in _YEARS_RE.findall(normalized))
    candidates.extend(int(value) for value in _YEARS_PLUS_RE.findall(normalized))
    for start_year, end_year in _PERIOD_RE.findall(normalized):
        candidates.append(int(end_year) - int(start_year))
    plausible = [value for value in candidates if 0 < value <= 50]
    return max(plausible) if plausible else 0


def extract_required_years(text: str) -> int | None:
    """Сколько лет требует критерий («не менее 3 лет» -> 3)."""
    normalized = normalize_text(text or "")
    match = _REQUIRED_YEARS_RE.search(normalized)
    if match is None:
        return None
    value = match.group(1) or match.group(2)
    return int(value) if value else None


class FakeLlmAdapter:
    """Детерминированная «модель» для тестов и демо.

    Полностью воспроизводима: один и тот же вход даёт один и тот же выход, без
    сети и ключей. Оценки выводятся из фактов резюме (годы опыта и найденные
    ключевые слова), а тексты сообщений — из переданных данных. Ни одного
    выдуманного факта о вакансии: то, чего нет в описании, так и говорится.
    """

    model_name = "fake-llm-v1"
    prompt_version = "v1"

    def __init__(self) -> None:
        self.screen_calls: list[ScreeningRequest] = []
        self.compose_calls: list[MessageRequest] = []
        self.reply_calls: list[ReplyAnalysisRequest] = []
        self.answer_calls: list[AnswerAssessmentRequest] = []

    # ---- LlmPort ---- #

    async def assess_answer(self, request: AnswerAssessmentRequest) -> AnswerAssessment:
        """Оценить ответ кандидата по критерию — детерминированно.

        Та же логика, что в ``adapters.llm.fake``: уклончивость, совпадение
        ключевых слов, объём. Дубль существует исторически; при слиянии
        реализаций останется одна.
        """
        self.answer_calls.append(request)
        text = (request.answer or "").strip()
        lowered = text.casefold()
        unclear_markers = (
            "не знаю",
            "не помню",
            "не уверен",
            "затрудняюсь",
            "без комментариев",
            "не хочу",
            "пропустить",
            "skip",
            "idk",
        )
        if len(text) < 3 or any(marker in lowered for marker in unclear_markers):
            return AnswerAssessment(score=0.0, evidence="", is_unclear=True)

        expected = set(_tokens(request.expectation)) | set(_tokens(request.evidence_hint))
        expected = {token for token in expected if len(token) >= 4}
        if not expected:
            return AnswerAssessment(score=50.0, evidence=text[:200])
        actual = set(_tokens(text))
        share = len(expected & actual) / len(expected)
        return AnswerAssessment(
            score=round(min(100.0, 30.0 + share * 70.0), 1),
            evidence=text[:200],
        )

    async def screen_resume(self, request: ScreeningRequest) -> ScreeningDraft:
        self.screen_calls.append(request)
        text_tokens = set(_tokens(request.resume_text))
        years = extract_years(request.resume_text)
        criteria: list[ScreeningCriterion] = []
        gaps: list[str] = []
        strengths: list[str] = []
        questions: list[str] = []

        for raw in request.criteria:
            key = str(raw.get("key") or raw.get("title") or "criterion")
            title = str(raw.get("title") or key)
            weight = float(raw.get("weight", 1.0) or 1.0)
            must_have = bool(raw.get("must_have", False))
            expectation = str(raw.get("expectation", "") or "")
            score, evidence = self._score_criterion(
                title=title,
                expectation=expectation,
                resume_text=request.resume_text,
                text_tokens=text_tokens,
                years=years,
            )
            is_gap = score < _FAKE_GAP_THRESHOLD
            criteria.append(
                ScreeningCriterion(
                    key=key,
                    title=title,
                    weight=weight,
                    score=score,
                    must_have=must_have,
                    evidence=evidence,
                    is_gap=is_gap,
                )
            )
            if is_gap:
                gaps.append(title)
                if len(questions) < 3:
                    questions.append(self._question_for(title))
            elif evidence:
                strengths.append(f"{title}: {evidence}")

        total_weight = sum(criterion.weight for criterion in criteria) or 1.0
        total = sum(criterion.score * criterion.weight for criterion in criteria) / total_weight
        summary = self._summary(request, years, total, gaps)
        return ScreeningDraft(
            total_score=round(total, 1),
            summary=summary,
            criteria=tuple(criteria),
            gaps=tuple(gaps),
            strengths=tuple(strengths[:3]),
            questions=tuple(questions),
            model=self.model_name,
        )

    async def compose_message(self, request: MessageRequest) -> MessageDraft:
        self.compose_calls.append(request)
        text = self._compose(request)
        return MessageDraft(
            text=text,
            model=self.model_name,
            prompt_version=self.prompt_version,
        )

    async def analyze_reply(self, request: ReplyAnalysisRequest) -> CandidateReply:
        self.reply_calls.append(request)
        intent, slot_index = self._classify(request.reply_text, len(request.proposed_slots))
        extracted: dict[str, Any] = {}
        if slot_index is not None:
            extracted["slot_number"] = slot_index + 1
        return CandidateReply(
            intent=intent,
            summary=f"Ответ разобран правилами fake-модели: {intent.value}",
            extracted=extracted,
            chosen_slot_index=slot_index,
            confidence=0.6 if intent is CandidateIntent.UNCLEAR else 0.85,
            raw_text=request.reply_text,
        )

    # ---- внутренняя «математика» ---- #

    def _score_criterion(
        self,
        *,
        title: str,
        expectation: str,
        resume_text: str,
        text_tokens: set[str],
        years: int,
    ) -> tuple[float, str]:
        required_years = extract_required_years(f"{title} {expectation}")
        if required_years is not None:
            return self._score_experience(resume_text, years, required_years)

        tokens = _significant_tokens(title) or _significant_tokens(expectation)
        if not tokens:
            # Критерий без различимых слов: судить не по чему, ставим нейтрально.
            return 50.0, ""

        hits = [token for token in tokens if _text_has_token(text_tokens, token)]
        if not hits:
            return _FAKE_NO_EVIDENCE_SCORE, ""
        evidence = _find_evidence(resume_text, hits[0])
        coverage = len(hits) / len(tokens)
        score = 55.0 + 40.0 * coverage
        if not evidence:
            # Нет цитаты из резюме — критерий не зачитывается полностью
            # (ARCHITECTURE.md, п. 6.4: защита от галлюцинаций).
            score = min(score, 40.0)
        return round(min(97.0, score), 1), evidence

    @staticmethod
    def _score_experience(resume_text: str, years: int, required_years: int) -> tuple[float, str]:
        evidence = _find_evidence(resume_text, "лет") or _find_evidence(resume_text, "год")
        if years <= 0:
            return 20.0, evidence
        if years >= required_years:
            return 95.0, evidence
        if years >= required_years - 1:
            return 70.0, evidence
        return 40.0, evidence

    def _summary(
        self,
        request: ScreeningRequest,
        years: int,
        total: float,
        gaps: Sequence[str],
    ) -> str:
        experience = f"Опыт: около {years} лет." if years else "Явно указанного опыта не нашлось."
        if gaps:
            gaps_text = ", ".join(gaps)
            return (
                f"{experience} Соответствие вакансии «{request.vacancy_title}»: "
                f"{total:.0f}/100. Пробелы: {gaps_text}."
            )
        return (
            f"{experience} Соответствие вакансии «{request.vacancy_title}»: "
            f"{total:.0f}/100. Пробелов по критериям нет."
        )

    @staticmethod
    def _question_for(title: str) -> str:
        return (
            f"Уточните, пожалуйста, по пункту «{title}»: какой у вас практический опыт "
            "и в каких задачах это применяли?"
        )

    def _compose(self, request: MessageRequest) -> str:
        context = request.context
        name = request.candidate_name or "кандидат"
        kind = request.kind
        slots = context.get("slots")
        slot_lines = "\n".join(str(line) for line in slots) if isinstance(slots, list) else ""
        questions = context.get("questions")
        questions_lines = (
            "\n".join(f"— {question}" for question in questions) if isinstance(questions, list) else ""
        )
        if kind == "invitation":
            return (
                f"Здравствуйте, {name}! Спасибо за отклик на вакансию «{request.vacancy_title}». "
                "Резюме посмотрели — хочется обсудить детали на коротком созвоне.\n"
                f"Вот время, где конфликта в календаре нет:\n{slot_lines}\n"
                "Напишите, пожалуйста, номер удобного варианта."
            )
        if kind == "slot_proposal":
            return (
                f"Здравствуйте, {name}! Вот варианты времени:\n{slot_lines}\n"
                "Напишите номер удобного — забронирую и пришлю подтверждение."
            )
        if kind == "clarifying_question":
            return (
                f"Здравствуйте, {name}! Спасибо за отклик. Чтобы не гадать по резюме, "
                f"уточните, пожалуйста:\n{questions_lines}"
            )
        if kind == "confirmation":
            moment = context.get("slot", "согласованное время")
            return (
                f"Готово, {name}! Записал вас на {moment}. "
                "Ссылку на встречу пришлёт рекрутер; если планы изменятся — напишите, перенесём."
            )
        if kind == "question_answer":
            return self._answer_question(request, str(context.get("question", "")))
        if kind == "rejection":
            return (
                f"Здравствуйте, {name}! Спасибо за отклик и потраченное время. "
                "По этой вакансии мы решили продолжить с другими кандидатами. "
                "Будем рады видеть вас в следующих подборках."
            )
        return f"Здравствуйте, {name}! Спасибо за ответ, вернусь с деталями."

    def _answer_question(self, request: MessageRequest, question: str) -> str:
        """Ответ строго по описанию вакансии.

        Если в описании нет ответа — так и говорим. Придумывать условия работы
        (зарплату, график, оформление) категорически нельзя.
        """
        description = str(request.context.get("vacancy_description", ""))
        name = request.candidate_name or "кандидат"
        best_sentence = ""
        best_score = 0
        question_tokens = set(_tokens(question))
        for sentence in re.split(r"(?<=[.!?])\s+", description):
            sentence_tokens = _tokens(sentence)
            score = 0
            for token in question_tokens:
                if len(token) < 4 or token in _CRITERION_STOPWORDS:
                    continue
                if any(candidate.startswith(_stem(token)) for candidate in sentence_tokens):
                    score += 1
            if score > best_score:
                best_score = score
                best_sentence = sentence.strip()
        if best_sentence and best_score > 0:
            return (
                f"{name}, по описанию вакансии: «{best_sentence}». "
                "Если нужны детали, которых здесь нет, — уточню у рекрутера."
            )
        return (
            f"{name}, в описании вакансии этой информации нет — уточню у рекрутера "
            "и вернусь с ответом, чтобы не сказать лишнего."
        )

    def _classify(self, text: str, slots_count: int) -> tuple[CandidateIntent, int | None]:
        """Разбор ответа кандидата правилами (без модели).

        Порядок проверок важен: сначала явный выбор слота, затем отказ, перенос,
        вопрос и только потом «интересно»; иначе «слот 2 не подходит» звучало бы
        как согласие.
        """
        normalized = normalize_text(text or "")
        slot_index = self._slot_index(normalized, slots_count)
        if slot_index is not None:
            return CandidateIntent.INTERESTED, slot_index

        if self._has_any(
            normalized,
            (
                "не интересно",
                "не интересен",
                "не актуально",
                "передумал",
                "отказ",
                "нет, спасибо",
                "not interested",
            ),
        ):
            return CandidateIntent.NOT_INTERESTED, None
        rescheduled = self._has_any(
            normalized,
            (
                "перенес",
                "перенест",
                "другое время",
                "другой день",
                "не могу в это время",
                "пересмотр",
                "другой слот",
                "reschedule",
                "another time",
            ),
        )
        reschedule_by_time = "не подходит" in normalized and self._has_any(
            normalized, ("время", "дата", "слот", "день")
        )
        if rescheduled or reschedule_by_time:
            return CandidateIntent.RESCHEDULE_REQUEST, None
        if "?" in normalized or self._has_any(
            normalized,
            ("подскажите", "расскажите", "какой", "какая", "когда", "сколько", "что за"),
        ):
            return CandidateIntent.QUESTION, None
        if self._has_any(
            normalized,
            (
                "интересно",
                "готов",
                "давайте",
                "подходит",
                "согласен",
                "согласна",
                "удобно",
                "беру",
                "yes",
            ),
        ):
            return CandidateIntent.INTERESTED, None
        if self._has_any(
            normalized,
            (
                "опыт",
                "работал",
                "работала",
                "знаю",
                "умею",
                "делал",
                "делала",
                "проект",
                "стек",
                "навык",
                "образование",
            ),
        ):
            return CandidateIntent.PROVIDED_INFO, None
        return CandidateIntent.UNCLEAR, None

    @staticmethod
    def _has_any(text: str, markers: Sequence[str]) -> bool:
        return any(marker in text for marker in markers)

    @staticmethod
    def _slot_index(text: str, slots_count: int) -> int | None:
        if slots_count <= 0:
            return None
        explicit = re.search(r"(?:вариант|слот|номер|№)\s*(\d{1,2})", text)
        if explicit is not None:
            number = int(explicit.group(1))
            return number - 1 if 1 <= number <= slots_count else None
        ordinals = {
            "первый": 1,
            "второй": 2,
            "третий": 3,
            "четвертый": 4,
            "пятый": 5,
            "first": 1,
            "second": 2,
            "third": 3,
        }
        for word, number in ordinals.items():
            if word in text and 1 <= number <= slots_count:
                return number - 1
        return None


# --------------------------------------------------------------------------- #
# Прочая инфраструктура для fake-контейнера
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class QueuedJob:
    """Запись о задаче, поставленной в fake-очередь."""

    job_id: str
    name: str
    payload: dict[str, Any]
    dedupe_key: str | None = None
    delay_s: float = 0.0


class InMemoryJobQueue:
    """Очередь задач в памяти.

    Задачи только записываются: тест и демо-CLI сами решают, когда их
    выполнять (через ``OutboxDispatcher.drain``). Так «inline»-режим не
    ломает транзакции, а поведение остаётся предсказуемым.
    """

    def __init__(self) -> None:
        self.jobs: list[QueuedJob] = []
        self._dedupe_keys: set[str] = set()
        self.started = False

    async def enqueue(
        self,
        job_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        delay_s: float = 0.0,
    ) -> str | None:
        if dedupe_key is not None:
            if dedupe_key in self._dedupe_keys:
                return None
            self._dedupe_keys.add(dedupe_key)
        job_id = f"job-{len(self.jobs) + 1}"
        self.jobs.append(
            QueuedJob(
                job_id=job_id,
                name=job_name,
                payload=dict(payload),
                dedupe_key=dedupe_key,
                delay_s=delay_s,
            )
        )
        return job_id

    async def start(self) -> None:
        self.started = True

    async def close(self) -> None:
        self.started = False

    def names(self) -> list[str]:
        return [job.name for job in self.jobs]

    def of(self, name: str) -> list[QueuedJob]:
        return [job for job in self.jobs if job.name == name]


class InMemoryLock:
    """Блокировка-заглушка: всегда успешно захватывается."""

    def acquire(
        self, key: str, *, ttl_s: float = 30.0, wait_s: float = 5.0
    ) -> AbstractAsyncContextManager[bool]:
        logger.debug("fake_lock_acquired", key=key, ttl_s=ttl_s, wait_s=wait_s)
        return _AlwaysAcquired()


class _AlwaysAcquired:
    async def __aenter__(self) -> bool:
        return True

    async def __aexit__(self, exc_type: Any, exc: Any, tb: Any) -> None:
        return None


class ImmediateRateLimiter:
    """Rate limiter-заглушка: не ждёт и не ограничивает."""

    async def acquire(self, key: str, *, rate_per_second: float, burst: int, wait: bool = True) -> float:
        logger.debug(
            "fake_rate_limit_acquired",
            key=key,
            rate_per_second=rate_per_second,
            burst=burst,
            wait=wait,
        )
        return 0.0


class StubHttpClient:
    """HTTP-заглушка: сервисы воронки в сеть не ходят."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        form: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = True,
        cache_ttl_s: float = 0.0,
        rate_limit_key: str | None = None,
        rate_limit_rps: float | None = None,
        bucket: str = "default",
    ) -> HttpResponse:
        self.calls.append((method, url))
        del params, json_body, form, headers, idempotent, cache_ttl_s
        del rate_limit_key, rate_limit_rps, bucket
        return HttpResponse(status_code=200, body={}, url=url)

    async def aclose(self) -> None:
        return None


class NoopCipher:
    """Шифрование ПДн включено, но в fake-контейнере это тождество."""

    def __init__(self) -> None:
        self.encrypted: list[str] = []

    def encrypt(self, value: str) -> str:
        self.encrypted.append(value)
        return value

    def decrypt(self, value: str) -> str:
        return value


@dataclass(slots=True)
class FakeClock:
    """Управляемые часы: фиксированное «сейчас» и ручной сдвиг.

    Локальный фолбэк: если в ``hirebot.infra.clock`` уже есть ``FakeClock``,
    используется он (см. ``_build_clock``). Фиксированное время делает слоты
    детерминированными, а тесты — воспроизводимыми.
    """

    current: datetime = field(default_factory=lambda: datetime(2026, 6, 1, 9, 0, tzinfo=UTC))

    def now(self) -> datetime:
        return self.current

    def advance(
        self,
        *,
        seconds: float = 0.0,
        minutes: float = 0.0,
        hours: float = 0.0,
        days: float = 0.0,
    ) -> datetime:
        self.current = self.current + timedelta(seconds=seconds, minutes=minutes, hours=hours, days=days)
        return self.current

    def set(self, value: datetime) -> None:
        self.current = value

    async def sleep(self, seconds: float) -> None:
        self.advance(seconds=seconds)


def _load_infra_fake_clock() -> type[Any] | None:
    """Попробовать взять ``FakeClock`` из инфраструктуры (если он уже есть).

    Импорт через ``importlib``: модуль ``hirebot.infra.clock`` пишет другая
    команда, и статический импорт сломал бы сборку до её готовности.
    """
    try:
        module = importlib.import_module("hirebot.infra.clock")
    except ImportError:
        return None
    candidate = getattr(module, "FakeClock", None)
    if isinstance(candidate, type) and hasattr(candidate, "advance"):
        return candidate
    return None


def _build_clock() -> Any:
    infra_clock = _load_infra_fake_clock()
    if infra_clock is not None:
        try:
            return infra_clock()
        except TypeError:
            logger.warning("infra_fake_clock_init_failed", clock=infra_clock.__name__)
    return FakeClock()


# --------------------------------------------------------------------------- #
# Сборка контейнера
# --------------------------------------------------------------------------- #


def build_fake_container(
    settings: Settings | None = None,
    *,
    seed_demo: bool = True,
) -> Container:
    """Собрать контейнер на fake-реализациях: без БД, Redis и сети.

    Используется unit-тестами и ``hirebot cli demo``. ``seed_demo=True``
    наполняет канал демонстрационными данными (вакансия с критериями и три
    отклика: сильный кандидат, слабый и кандидат с закрытым резюме), поэтому
    демо-прогон работает сразу. Тесты, которым нужен пустой контейнер,
    передают ``seed_demo=False``.
    """
    resolved = settings or Settings(env="test", _env_file=None)
    clock = _build_clock()
    store = InMemoryStore()
    hh_channel = FakeChannelAdapter(Channel.HH, clock=clock)
    avito_channel = FakeChannelAdapter(Channel.AVITO, clock=clock)
    calendar = FakeCalendarAdapter(clock=clock)
    llm = FakeLlmAdapter()
    queue = InMemoryJobQueue()

    if seed_demo:
        seed_demo_data(
            store=store,
            hh_channel=hh_channel,
        )

    return Container(
        settings=resolved,
        uow_factory=_in_memory_uow_factory(store, clock),
        http=StubHttpClient(),
        cache=_memory_cache(clock),
        locks=InMemoryLock(),
        rate_limiter=ImmediateRateLimiter(),
        queue=queue,
        clock=clock,
        llm=llm,
        cipher=NoopCipher(),
        channels={Channel.HH: hh_channel, Channel.AVITO: avito_channel},
        calendar=calendar,
    )


def seed_demo_data(*, store: InMemoryStore, hh_channel: FakeChannelAdapter) -> None:
    """Наполнить fake-контейнер демонстрационными данными.

    Вакансия кладётся сразу в БД (с критериями скоринга) и в канал: критерии
    настраивает рекрутер уже в нашей системе, а из отклика их взять негде.
    """
    vacancy = Vacancy(
        id=new_id(),
        channel=Channel.HH,
        external_id=DEMO_VACANCY_EXTERNAL_ID,
        title="Python-разработчик",
        company_id=DEFAULT_COMPANY_ID,
        description=DEMO_VACANCY_DESCRIPTION,
        city="Москва",
        criteria=[dict(criterion) for criterion in DEMO_CRITERIA],
    )
    store.vacancies[vacancy.id] = vacancy
    store.vacancy_ids[(vacancy.channel, vacancy.external_id)] = vacancy.id

    hh_channel.add_vacancy(
        VacancyPayload(
            external_id=DEMO_VACANCY_EXTERNAL_ID,
            title=vacancy.title,
            description=DEMO_VACANCY_DESCRIPTION,
            city="Москва",
        )
    )
    for response in demo_responses():
        hh_channel.add_response(response)
    hh_channel.add_resume(
        ResumePayload(
            external_id="hh-resume-1001",
            text=DEMO_RESUME_STRONG,
            candidate_name="Алексей Петров",
            parsed={"total_experience_months": 60, "timezone": "Europe/Moscow"},
        )
    )
    hh_channel.add_resume(
        ResumePayload(
            external_id="hh-resume-1002",
            text=DEMO_RESUME_WEAK,
            candidate_name="Мария Смирнова",
            parsed={"total_experience_months": 12, "timezone": "Europe/Moscow"},
        )
    )
    hh_channel.add_resume(
        ResumePayload(
            external_id="hh-resume-1003",
            text=DEMO_RESUME_DENIED,
            candidate_name="Иван Кузнецов",
        )
    )
    # Третий кандидат — пример штатной деградации: резюме закрыто политикой
    # площадки, и диалог должен уйти человеку, а не упасть.
    hh_channel.deny_resume("hh-resume-1003")


def _memory_cache(clock: Any) -> Any:
    """Кэш тестового контура — настоящий ``MemoryCache``, а не ``NullCache``.

    ``NullCache.incr`` всегда возвращает 1, поэтому дедупликация вебхуков
    (``claim_event``) в fake-контуре была бы отключена, и повторная доставка
    одного уведомления запускала бы обработку заново. ``MemoryCache`` считает
    счётчики так же, как боевой Redis, поэтому контур воспроизводит боевое
    поведение.
    """
    return MemoryCache(clock=clock)


def _in_memory_uow_factory(store: InMemoryStore, clock: Any) -> UowFactory:
    """Фабрика Unit of Work для fake-контейнера.

    Приведение типа нужно потому, что ``__aenter__`` конкретного класса
    возвращает ``InMemoryUnitOfWork``, а порт ожидает
    ``AbstractAsyncContextManager[UnitOfWork]``.
    """

    def factory() -> AbstractAsyncContextManager[UnitOfWork]:
        concrete = InMemoryUnitOfWork(store, clock=clock)
        return cast(AbstractAsyncContextManager[UnitOfWork], concrete)

    return factory


def fake_channel(container: Container, channel: Channel = Channel.HH) -> FakeChannelAdapter:
    """Привести адаптер канала к ``FakeChannelAdapter`` (для тестов)."""
    adapter = container.channel(channel)
    if not isinstance(adapter, FakeChannelAdapter):
        msg = "Контейнер собран не на FakeChannelAdapter"
        raise TypeError(msg)
    return adapter


def fake_calendar(container: Container) -> FakeCalendarAdapter:
    """Привести календарь к ``FakeCalendarAdapter`` (для тестов)."""
    calendar = container.calendar
    if not isinstance(calendar, FakeCalendarAdapter):
        msg = "Контейнер собран не на FakeCalendarAdapter"
        raise TypeError(msg)
    return calendar


def fake_llm(container: Container) -> FakeLlmAdapter:
    """Привести LLM к ``FakeLlmAdapter`` (для тестов)."""
    llm = container.llm
    if not isinstance(llm, FakeLlmAdapter):
        msg = "Контейнер собран не на FakeLlmAdapter"
        raise TypeError(msg)
    return llm


def fake_queue(container: Container) -> InMemoryJobQueue:
    """Привести очередь к ``InMemoryJobQueue`` (для тестов)."""
    queue = container.queue
    if not isinstance(queue, InMemoryJobQueue):
        msg = "Контейнер собран не на InMemoryJobQueue"
        raise TypeError(msg)
    return queue


@asynccontextmanager
async def opened_store(container: Container) -> AsyncIterator[InMemoryStore]:
    """Открыть хранилище fake-контейнера как асинхронный контекст."""
    async with container.uow_factory() as uow:
        yield cast(InMemoryUnitOfWork, uow).store


__all__ = [
    "DEMO_CRITERIA",
    "DEMO_RESUME_DENIED",
    "DEMO_RESUME_STRONG",
    "DEMO_RESUME_WEAK",
    "DEMO_VACANCY_DESCRIPTION",
    "DEMO_VACANCY_EXTERNAL_ID",
    "FAKE_CHANNEL_ACTIONS",
    "FakeCalendarAdapter",
    "FakeChannelAdapter",
    "FakeClock",
    "FakeLlmAdapter",
    "FakeMeeting",
    "ImmediateRateLimiter",
    "InMemoryBookingRepository",
    "InMemoryCandidateRepository",
    "InMemoryConversationRepository",
    "InMemoryIdempotencyRepository",
    "InMemoryJobQueue",
    "InMemoryLock",
    "InMemoryMessageRepository",
    "InMemoryOutboxRepository",
    "InMemoryResumeRepository",
    "InMemoryScreeningRepository",
    "InMemoryStore",
    "InMemoryUnitOfWork",
    "InMemoryVacancyRepository",
    "NoopCipher",
    "QueuedJob",
    "SentMessageRecord",
    "StubHttpClient",
    "build_fake_container",
    "demo_responses",
    "extract_required_years",
    "extract_years",
    "fake_calendar",
    "fake_channel",
    "fake_llm",
    "fake_queue",
    "opened_store",
    "seed_demo_data",
]
