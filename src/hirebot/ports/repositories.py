"""Порты репозиториев и Unit of Work.

Почему Unit of Work, а не «просто сессия SQLAlchemy» в каждом сервисе:
сценарий «сохранить сообщение + перевести диалог в новое состояние + положить
задачу в outbox» обязан быть одной транзакцией. Иначе воркер успевает увидеть
неконсистентное состояние и отправить кандидату не то, что планировалось.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from datetime import datetime
from types import TracebackType
from typing import Any, Protocol, Self, runtime_checkable
from uuid import UUID

from hirebot.domain.budget import PaidAction
from hirebot.domain.entities import (
    Booking,
    Candidate,
    Company,
    Conversation,
    DraftMessage,
    Message,
    OutboxEntry,
    QuestionAnswer,
    Resume,
    ScreeningResult,
    Vacancy,
)
from hirebot.domain.enums import Channel, ConversationState, OutboxStatus
from hirebot.domain.scoring import ScoringProfile


@runtime_checkable
class CompanyRepository(Protocol):
    """Компании-арендаторы.

    Нужен, чтобы создавать данные, не прибегая к SQL напрямую: у вакансий,
    кандидатов и диалогов есть внешний ключ на компанию.
    """

    async def upsert(self, company: Company) -> Company: ...
    async def get(self, company_id: UUID) -> Company | None: ...
    async def get_by_slug(self, slug: str) -> Company | None: ...


@runtime_checkable
class VacancyRepository(Protocol):
    async def upsert(self, vacancy: Vacancy) -> Vacancy: ...
    async def get(self, vacancy_id: UUID) -> Vacancy | None: ...
    async def get_by_external(self, channel: Channel, external_id: str) -> Vacancy | None: ...
    async def list_active(self, company_id: UUID) -> Sequence[Vacancy]: ...
    async def delete(self, vacancy_id: UUID) -> bool:
        """Удалить вакансию физически. Возвращает False, если её нет."""
        ...


@runtime_checkable
class CandidateRepository(Protocol):
    async def upsert(self, candidate: Candidate) -> Candidate: ...
    async def get(self, candidate_id: UUID) -> Candidate | None: ...
    async def get_by_external(self, channel: Channel, external_id: str) -> Candidate | None: ...


@runtime_checkable
class ResumeRepository(Protocol):
    async def save(self, resume: Resume) -> Resume: ...
    async def get(self, resume_id: UUID) -> Resume | None: ...
    async def get_latest_for_candidate(self, candidate_id: UUID) -> Resume | None: ...


@runtime_checkable
class ConversationRepository(Protocol):
    async def upsert(self, conversation: Conversation) -> Conversation: ...
    async def get(self, conversation_id: UUID) -> Conversation | None: ...
    async def get_by_external(self, channel: Channel, external_id: str) -> Conversation | None: ...
    async def get_with_lock(self, conversation_id: UUID) -> Conversation | None:
        """Прочитать диалог с блокировкой строки.

        Нужна, чтобы два параллельных воркера не написали кандидату дважды.
        """
        ...

    async def count_for_vacancy(self, vacancy_id: UUID) -> int:
        """Сколько диалогов связано с вакансией (для безопасного удаления)."""
        ...

    async def list_by_state(
        self, states: Sequence[ConversationState], *, limit: int = 100
    ) -> Sequence[Conversation]: ...


@runtime_checkable
class MessageRepository(Protocol):
    async def add(self, message: Message) -> Message: ...
    async def list_for_conversation(self, conversation_id: UUID, *, limit: int = 50) -> Sequence[Message]: ...
    async def exists_external_id(self, conversation_id: UUID, external_id: str) -> bool: ...


@runtime_checkable
class ScreeningRepository(Protocol):
    async def save(self, result: ScreeningResult) -> ScreeningResult: ...
    async def get_latest(self, candidate_id: UUID, vacancy_id: UUID) -> ScreeningResult | None: ...
    async def get_by_content_hash(self, content_hash: str) -> ScreeningResult | None: ...


@runtime_checkable
class BookingRepository(Protocol):
    async def save(self, booking: Booking) -> Booking: ...
    async def get(self, booking_id: UUID) -> Booking | None: ...
    async def get_by_idempotency_key(self, key: str) -> Booking | None: ...
    async def get_for_conversation(self, conversation_id: UUID) -> Booking | None:
        """Последняя (актуальная) бронь по диалогу.

        Нужна карточке кандидата в API и в демо-прогоне: показать назначенное
        собеседование, не зная ключа идемпотентности и идентификатора менеджера.
        """
        ...

    async def list_active_for_manager(
        self, manager_id: str, *, start: datetime, end: datetime
    ) -> Sequence[Booking]: ...

    async def find_active_overlap(self, manager_id: str, *, start: datetime, end: datetime) -> Booking | None:
        """Первая активная бронь менеджера, пересекающаяся с ``[start, end)``.

        Нужна для транзакционного захвата слота: проверка выполняется в той же
        транзакции, что и сохранение брони, — иначе занятость, появившаяся между
        подбором слота и созданием встречи, осталась бы незамеченной. Гонку
        окончательно закрывает частичный уникальный индекс
        ``bookings(manager_id, starts_at)`` для активных статусов.
        """
        ...


@runtime_checkable
class MessageDraftRepository(Protocol):
    """Черновики сообщений для очереди подтверждения.

    Инвариант: у диалога не может быть больше одного черновика в статусе
    ``PENDING``. Новый черновик вытесняет предыдущий (``SUPERSEDED``), иначе
    рекрутер одобрил бы устаревшее сообщение.
    """

    async def add(self, draft: DraftMessage) -> DraftMessage:
        """Сохранить черновик, вытеснив предыдущий ожидающий по этому диалогу."""
        ...

    async def get(self, draft_id: UUID) -> DraftMessage | None: ...
    async def get_pending_for_conversation(self, conversation_id: UUID) -> DraftMessage | None: ...
    async def list_pending(self, *, limit: int = 100) -> Sequence[DraftMessage]: ...
    async def save(self, draft: DraftMessage) -> DraftMessage: ...


@runtime_checkable
class ScoringProfileRepository(Protocol):
    """Профили скоринга: версии, публикация, откат.

    Инвариант: у вакансии не больше одной «живой» версии (``active`` или
    ``shadow``). Он держится частичным уникальным индексом в БД, а не только
    кодом: два параллельных запроса иначе сделали бы решение лотереей.
    """

    async def add(self, profile: ScoringProfile) -> ScoringProfile:
        """Сохранить черновик профиля."""
        ...

    async def get(self, profile_id: UUID) -> ScoringProfile | None: ...
    async def get_live(self, vacancy_id: UUID) -> ScoringProfile | None:
        """Профиль, применяемый к новым кандидатам (active или shadow)."""
        ...

    async def get_latest_version(self, vacancy_id: UUID) -> ScoringProfile | None: ...
    async def list_versions(self, vacancy_id: UUID, *, limit: int = 50) -> Sequence[ScoringProfile]: ...
    async def save(self, profile: ScoringProfile) -> ScoringProfile: ...
    async def publish(self, profile_id: UUID) -> ScoringProfile:
        """Сделать версию живой, архивировав прежнюю — в одной транзакции."""
        ...

    async def activate_version(self, vacancy_id: UUID, version: int) -> ScoringProfile:
        """Откатить вакансию на другую версию (прежняя уходит в архив)."""
        ...


@runtime_checkable
class QuestionAnswerRepository(Protocol):
    """Ответы кандидатов на уточняющие вопросы."""

    async def add(self, answer: QuestionAnswer) -> QuestionAnswer: ...
    async def list_for_conversation(self, conversation_id: UUID) -> Sequence[QuestionAnswer]: ...
    async def latest_for_criterion(
        self, conversation_id: UUID, criterion_key: str
    ) -> QuestionAnswer | None: ...


@runtime_checkable
class PaidActionRepository(Protocol):
    """Журнал платных действий и сумма расходов за период."""

    async def add(self, action: PaidAction) -> PaidAction: ...
    async def spent_since(self, company_id: UUID, since: datetime) -> float:
        """Сумма списаний компании начиная с момента ``since``."""
        ...


@runtime_checkable
class OutboxRepository(Protocol):
    async def add(self, entry: OutboxEntry) -> OutboxEntry: ...
    async def claim_batch(self, *, limit: int, now: datetime) -> Sequence[OutboxEntry]:
        """Забрать пачку задач в работу с блокировкой (``FOR UPDATE SKIP LOCKED``)."""
        ...

    async def mark(self, entry_id: UUID, status: OutboxStatus, *, error: str | None = None) -> None: ...


@runtime_checkable
class IdempotencyRepository(Protocol):
    async def acquire(self, key: str, *, scope: str, ttl_hours: int = 72) -> bool:
        """Захватить ключ идемпотентности.

        Возвращает ``False``, если ключ уже был — значит действие уже выполнено
        и повторять его нельзя.
        """
        ...


@dataclass(slots=True, frozen=True)
class PurgeStats:
    """Сколько записей удалено за проход по срокам хранения."""

    resumes: int = 0
    messages: int = 0
    screenings: int = 0
    drafts: int = 0
    audit: int = 0

    @property
    def total(self) -> int:
        return self.resumes + self.messages + self.screenings + self.drafts + self.audit

    def as_dict(self) -> dict[str, int]:
        return {
            "resumes": self.resumes,
            "messages": self.messages,
            "screenings": self.screenings,
            "drafts": self.drafts,
            "audit": self.audit,
        }


@runtime_checkable
class PrivacyRepository(Protocol):
    """Удаление и выгрузка персональных данных (152-ФЗ).

    Вынесено отдельным портом, а не методами каждого репозитория: это не часть
    бизнес-воронки, а обязанность оператора данных. Реализация удаляет **ПДн**,
    но не ломает ссылочную целостность воронки.
    """

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
        """Удалить данные, срок хранения которых истёк."""
        ...

    async def export_candidate(self, candidate_id: UUID) -> dict[str, Any] | None:
        """Выгрузить все данные кандидата (право на доступ к своим данным)."""
        ...

    async def erase_candidate(self, candidate_id: UUID) -> bool:
        """Удалить данные кандидата целиком (право на удаление)."""
        ...


@runtime_checkable
class UnitOfWork(Protocol):
    """Транзакционная граница.

    Использование::

        async with uow:
            conv = await uow.conversations.get_with_lock(cid)
            await uow.messages.add(msg)
            await uow.outbox.add(entry)
            await uow.commit()
    """

    vacancies: VacancyRepository
    companies: CompanyRepository
    profiles: ScoringProfileRepository
    paid_actions: PaidActionRepository
    answers: QuestionAnswerRepository
    candidates: CandidateRepository
    resumes: ResumeRepository
    conversations: ConversationRepository
    messages: MessageRepository
    drafts: MessageDraftRepository
    screenings: ScreeningRepository
    bookings: BookingRepository
    outbox: OutboxRepository
    idempotency: IdempotencyRepository
    privacy: PrivacyRepository

    async def __aenter__(self) -> Self: ...
    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None: ...
    async def commit(self) -> None: ...
    async def rollback(self) -> None: ...
