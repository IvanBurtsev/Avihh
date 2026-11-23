"""Unit of Work: транзакционная граница поверх одной сессии SQLAlchemy.

Зачем UoW, а не «просто сессия» в каждом сервисе: сценарий «сохранить сообщение +
перевести диалог в новое состояние + положить задачу в outbox» обязан быть
**одной** транзакцией. Иначе воркер успевает увидеть неконсистентное состояние и
отправляет кандидату не то, что планировалось.

Семантика выхода:

* штатное завершение ``async with`` → ``commit()`` (забытый ``commit()`` не
  приводит к молчаливой потере работы);
* исключение внутри блока → ``rollback()``;
* в любом случае сессия «сбрасывается»: ``expunge_all()`` + ``close()``,
  чтобы объекты не оставались привязанными к закрытой сессии.

Повторный вход в тот же объект создаёт новую сессию: UoW — короткоживущий
объект, его не нужно переиспользовать между обработками задач.
"""

from __future__ import annotations

from types import TracebackType
from typing import Self

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hirebot.infra.db.repositories import (
    SqlAuditLogRepository,
    SqlBookingRepository,
    SqlCandidateRepository,
    SqlCompanyRepository,
    SqlConversationRepository,
    SqlIdempotencyRepository,
    SqlMessageDraftRepository,
    SqlMessageRepository,
    SqlOutboxRepository,
    SqlPaidActionRepository,
    SqlPrivacyRepository,
    SqlQuestionAnswerRepository,
    SqlResumeRepository,
    SqlScoringProfileRepository,
    SqlScreeningRepository,
    SqlVacancyRepository,
)
from hirebot.infra.security.crypto import FieldCipher
from hirebot.logging_setup import get_logger
from hirebot.ports.repositories import (
    BookingRepository,
    CandidateRepository,
    CompanyRepository,
    ConversationRepository,
    IdempotencyRepository,
    MessageDraftRepository,
    MessageRepository,
    OutboxRepository,
    PaidActionRepository,
    PrivacyRepository,
    QuestionAnswerRepository,
    ResumeRepository,
    ScoringProfileRepository,
    ScreeningRepository,
    VacancyRepository,
)

logger = get_logger(__name__)


class SqlAlchemyUnitOfWork:
    """Реализация ``UnitOfWork`` на одной ``AsyncSession``.

    Использование::

        async with uow:
            conversation = await uow.conversations.get_with_lock(cid)
            await uow.messages.add(message)
            await uow.outbox.add(entry)
            await uow.commit()
    """

    def __init__(self, session_factory: async_sessionmaker[AsyncSession], cipher: FieldCipher) -> None:
        self._session_factory = session_factory
        self._cipher = cipher
        self._session: AsyncSession | None = None

        # Аннотации без присваивания: атрибуты создаются в ``__aenter__``,
        # но тип был известен заранее (иначе mypy потерял бы типизацию).
        self.vacancies: VacancyRepository
        self.companies: CompanyRepository
        self.profiles: ScoringProfileRepository
        self.paid_actions: PaidActionRepository
        self.answers: QuestionAnswerRepository
        self.candidates: CandidateRepository
        self.resumes: ResumeRepository
        self.conversations: ConversationRepository
        self.messages: MessageRepository
        self.drafts: MessageDraftRepository
        self.screenings: ScreeningRepository
        self.bookings: BookingRepository
        self.outbox: OutboxRepository
        self.idempotency: IdempotencyRepository
        self.privacy: PrivacyRepository
        # Дополнительно к порту: audit_log пишется из сервисов напрямую.
        self.audit: SqlAuditLogRepository

    @property
    def session(self) -> AsyncSession:
        """Текущая сессия (нужна для диагностики и тестов)."""
        return self._require_session()

    async def __aenter__(self) -> Self:
        session = self._session_factory()
        self._session = session
        self.vacancies = SqlVacancyRepository(session, self._cipher)
        self.companies = SqlCompanyRepository(session, self._cipher)
        self.profiles = SqlScoringProfileRepository(session, self._cipher)
        self.paid_actions = SqlPaidActionRepository(session, self._cipher)
        self.answers = SqlQuestionAnswerRepository(session, self._cipher)
        self.candidates = SqlCandidateRepository(session, self._cipher)
        self.resumes = SqlResumeRepository(session, self._cipher)
        self.conversations = SqlConversationRepository(session, self._cipher)
        self.messages = SqlMessageRepository(session, self._cipher)
        self.drafts = SqlMessageDraftRepository(session, self._cipher)
        self.screenings = SqlScreeningRepository(session, self._cipher)
        self.bookings = SqlBookingRepository(session, self._cipher)
        self.outbox = SqlOutboxRepository(session, self._cipher)
        self.idempotency = SqlIdempotencyRepository(session, self._cipher)
        self.privacy = SqlPrivacyRepository(session, self._cipher)
        self.audit = SqlAuditLogRepository(session, self._cipher)
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        # ARG002: аргументы нужны по протоколу ``AbstractAsyncContextManager``.
        del exc, tb
        session = self._session
        if session is None:
            return
        try:
            if exc_type is not None:
                await session.rollback()
                logger.warning("uow_rollback", reason=exc_type.__name__)
            else:
                await session.commit()
        except Exception:
            # Откат после неудачного коммита обязателен: иначе следующий запрос
            # в этой же сессии получит «current transaction is aborted».
            await session.rollback()
            raise
        finally:
            await self._reset(session)

    async def commit(self) -> None:
        await self._require_session().commit()

    async def rollback(self) -> None:
        await self._require_session().rollback()

    def _require_session(self) -> AsyncSession:
        if self._session is None:
            msg = (
                "UnitOfWork ещё не открыт: используйте 'async with uow:'. "
                "После выхода из блока сессия освобождается."
            )
            raise RuntimeError(msg)
        return self._session

    async def _reset(self, session: AsyncSession) -> None:
        """Отвязать объекты и закрыть сессию."""
        try:
            session.expunge_all()
        finally:
            await session.close()
            self._session = None


__all__ = ["SqlAlchemyUnitOfWork"]
