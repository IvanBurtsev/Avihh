"""Репозитории: единственное место, где домен встречается с SQL.

Правила, которые здесь соблюдаются буквально:

* **Только параметризованные запросы.** Никаких f-string в SQL и никакого
  ``text()`` со склейкой: значения передаются как bind-параметры, иначе одна
  строка из резюме кандидата может превратиться в SQL-инъекцию.
* **Все списки пагинированы** (``limit``/``offset``). Списков без ограничения
  здесь нет вообще: у ``messages`` и ``audit_log`` рост измеряется миллионами.
* **Никаких N+1.** Доменные сущности плоские (связи — только идентификаторы),
  поэтому обычные выборки не тянут связи. Там, где связи реально нужны,
  используется ``selectinload`` (см. :meth:`SqlConversationRepository.list_with_details`),
  а у моделей стоит ``lazy="raise"`` — случайная ленивая загрузка падает сразу,
  а не в середине обработки отклика.
* **Блокировки — по диалекту.** ``SELECT ... FOR UPDATE`` есть в PostgreSQL, но
  не имеет смысла в SQLite (там блокировка файла/базы, а не строки).
* **Шифрование ПДн — здесь.** Домен получает открытые значения; в БД уходят
  только шифротексты и blind index-ы.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, TypeVar, cast
from uuid import UUID, uuid4

from sqlalchemy import Select, exists, func, select
from sqlalchemy import delete as sa_delete
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.dialects.sqlite import insert as sqlite_insert
from sqlalchemy.engine import CursorResult
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

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
from hirebot.domain.enums import (
    BookingStatus,
    Channel,
    ConversationState,
    DraftStatus,
    OutboxStatus,
    ProfileStatus,
)
from hirebot.domain.errors import NotFound, SlotConflict
from hirebot.domain.scoring import ScoringProfile
from hirebot.infra.db import mappers
from hirebot.infra.db.models import (
    AuditLogRow,
    BookingRow,
    CandidateRow,
    CompanyRow,
    ConversationRow,
    IdempotencyKeyRow,
    MessageDraftRow,
    MessageRow,
    OutboxRow,
    PaidActionRow,
    ProfileCriterionRow,
    ProfileQuestionRow,
    QuestionAnswerRow,
    ResumeRow,
    ScoringProfileRow,
    ScreeningRow,
    VacancyRow,
    utcnow,
)
from hirebot.infra.security.crypto import FieldCipher
from hirebot.logging_setup import get_logger
from hirebot.ports.repositories import PurgeStats

logger = get_logger(__name__)

#: Диалекты, где ``SELECT ... FOR UPDATE`` реально блокирует строку.
FOR_UPDATE_DIALECTS: frozenset[str] = frozenset({"postgresql", "mysql", "mariadb", "oracle"})

#: Максимальная длина текста ошибки, которую сохраняем в ``outbox.last_error``.
#: Полный traceback в БД — это мегабайты на инцидент и ПДн в тексте исключения.
MAX_ERROR_LEN = 2000

#: Статусы брони, которые считаются «активными» при проверке занятости.
ACTIVE_BOOKING_STATUSES: tuple[str, ...] = (
    BookingStatus.PROPOSED.value,
    BookingStatus.CONFIRMED.value,
)

_RowT = TypeVar("_RowT")


def _clamp_pagination(limit: int, offset: int) -> tuple[int, int]:
    """Привести пагинацию к безопасным значениям."""
    return max(1, limit), max(0, offset)


class _BaseRepository:
    """Общая часть: сессия, шифровальщик и определение диалекта."""

    def __init__(self, session: AsyncSession, cipher: FieldCipher) -> None:
        self._session = session
        self._cipher = cipher

    @property
    def _dialect(self) -> str:
        """Имя диалекта текущего подключения (``postgresql``/``sqlite``)."""
        return self._session.get_bind().dialect.name

    async def _first(self, stmt: Select[_RowT]) -> _RowT | None:
        return cast("_RowT | None", await self._session.scalar(stmt))

    async def _all(self, stmt: Select[_RowT]) -> list[_RowT]:
        return list((await self._session.scalars(stmt)).all())


# --- Company -----------------------------------------------------------------


class SqlCompanyRepository(_BaseRepository):
    """Компании. Ключ дедупликации — ``slug``."""

    async def upsert(self, company: Company) -> Company:
        row = await self._first(select(CompanyRow).where(CompanyRow.slug == company.slug))
        if row is None:
            row = await self._session.get(CompanyRow, company.id)
        if row is None:
            row = CompanyRow(id=company.id)
            mappers.apply_company(row, company)
            self._session.add(row)
        else:
            mappers.apply_company(row, company)
        await self._session.flush()
        return mappers.company_from_row(row)

    async def get(self, company_id: UUID) -> Company | None:
        row = await self._session.get(CompanyRow, company_id)
        return mappers.company_from_row(row) if row is not None else None

    async def get_by_slug(self, slug: str) -> Company | None:
        row = await self._first(select(CompanyRow).where(CompanyRow.slug == slug))
        return mappers.company_from_row(row) if row is not None else None


# --- Vacancy -----------------------------------------------------------------


class SqlVacancyRepository(_BaseRepository):
    """Вакансии. Ключ дедупликации — ``(channel, external_id)``."""

    async def upsert(self, vacancy: Vacancy) -> Vacancy:
        row = await self._first(
            select(VacancyRow).where(
                VacancyRow.channel == vacancy.channel.value,
                VacancyRow.external_id == vacancy.external_id,
            )
        )
        if row is None:
            row = await self._session.get(VacancyRow, vacancy.id)
        if row is None:
            row = VacancyRow(id=vacancy.id)
            mappers.apply_vacancy(row, vacancy)
            self._session.add(row)
        else:
            mappers.apply_vacancy(row, vacancy)
            row.updated_at = utcnow()
        await self._session.flush()
        return mappers.vacancy_from_row(row)

    async def get(self, vacancy_id: UUID) -> Vacancy | None:
        row = await self._session.get(VacancyRow, vacancy_id)
        return mappers.vacancy_from_row(row) if row is not None else None

    async def get_by_external(self, channel: Channel, external_id: str) -> Vacancy | None:
        row = await self._first(
            select(VacancyRow).where(
                VacancyRow.channel == channel.value,
                VacancyRow.external_id == external_id,
            )
        )
        return mappers.vacancy_from_row(row) if row is not None else None

    async def delete(self, vacancy_id: UUID) -> bool:
        """Удалить вакансию. Связанные записи уходят каскадом на уровне БД."""
        row = await self._session.get(VacancyRow, vacancy_id)
        if row is None:
            return False
        await self._session.delete(row)
        await self._session.flush()
        return True

    async def list_active(self, company_id: UUID, *, limit: int = 100, offset: int = 0) -> Sequence[Vacancy]:
        limit, offset = _clamp_pagination(limit, offset)
        rows = await self._all(
            select(VacancyRow)
            .where(VacancyRow.company_id == company_id, VacancyRow.is_active.is_(True))
            .order_by(VacancyRow.created_at.desc(), VacancyRow.id)
            .limit(limit)
            .offset(offset)
        )
        return [mappers.vacancy_from_row(row) for row in rows]


# --- Candidate ---------------------------------------------------------------


class SqlCandidateRepository(_BaseRepository):
    """Кандидаты. Контакты шифруются, поиск идёт по blind index."""

    async def upsert(self, candidate: Candidate) -> Candidate:
        row = await self._first(
            select(CandidateRow).where(
                CandidateRow.channel == candidate.channel.value,
                CandidateRow.external_id == candidate.external_id,
            )
        )
        if row is None:
            row = await self._session.get(CandidateRow, candidate.id)
        if row is None:
            row = CandidateRow(id=candidate.id)
            mappers.apply_candidate(row, candidate, self._cipher)
            self._session.add(row)
        else:
            mappers.apply_candidate(row, candidate, self._cipher)
        await self._session.flush()
        return mappers.candidate_from_row(row, self._cipher)

    async def get(self, candidate_id: UUID) -> Candidate | None:
        row = await self._session.get(CandidateRow, candidate_id)
        return mappers.candidate_from_row(row, self._cipher) if row is not None else None

    async def get_by_external(self, channel: Channel, external_id: str) -> Candidate | None:
        row = await self._first(
            select(CandidateRow).where(
                CandidateRow.channel == channel.value,
                CandidateRow.external_id == external_id,
            )
        )
        return mappers.candidate_from_row(row, self._cipher) if row is not None else None

    async def find_by_phone(
        self, phone: str, *, company_id: UUID | None = None, limit: int = 10
    ) -> Sequence[Candidate]:
        """Найти кандидатов по телефону через blind index (без расшифровки всех строк)."""
        limit, _ = _clamp_pagination(limit, 0)
        stmt = select(CandidateRow).where(CandidateRow.phone_index == self._cipher.blind_index(phone))
        if company_id is not None:
            stmt = stmt.where(CandidateRow.company_id == company_id)
        rows = await self._all(stmt.order_by(CandidateRow.created_at.desc()).limit(limit))
        return [mappers.candidate_from_row(row, self._cipher) for row in rows]

    async def find_by_email(
        self, email: str, *, company_id: UUID | None = None, limit: int = 10
    ) -> Sequence[Candidate]:
        """Найти кандидатов по e-mail через blind index."""
        limit, _ = _clamp_pagination(limit, 0)
        stmt = select(CandidateRow).where(CandidateRow.email_index == self._cipher.blind_index(email))
        if company_id is not None:
            stmt = stmt.where(CandidateRow.company_id == company_id)
        rows = await self._all(stmt.order_by(CandidateRow.created_at.desc()).limit(limit))
        return [mappers.candidate_from_row(row, self._cipher) for row in rows]


# --- Resume ------------------------------------------------------------------


class SqlResumeRepository(_BaseRepository):
    """Резюме. Текст резюме — ПДн, хранится только шифротекстом."""

    async def save(self, resume: Resume) -> Resume:
        row = await self._session.get(ResumeRow, resume.id)
        if row is None:
            row = ResumeRow(id=resume.id)
            mappers.apply_resume(row, resume, self._cipher)
            self._session.add(row)
        else:
            mappers.apply_resume(row, resume, self._cipher)
        await self._session.flush()
        return mappers.resume_from_row(row, self._cipher)

    async def get(self, resume_id: UUID) -> Resume | None:
        row = await self._session.get(ResumeRow, resume_id)
        return mappers.resume_from_row(row, self._cipher) if row is not None else None

    async def get_latest_for_candidate(self, candidate_id: UUID) -> Resume | None:
        row = await self._first(
            select(ResumeRow)
            .where(ResumeRow.candidate_id == candidate_id)
            .order_by(ResumeRow.fetched_at.desc(), ResumeRow.id.desc())
            .limit(1)
        )
        return mappers.resume_from_row(row, self._cipher) if row is not None else None


# --- Conversation ------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class ConversationDetails:
    """Диалог вместе со связанными сущностями (загружается без N+1)."""

    conversation: Conversation
    candidate: Candidate | None
    vacancy: Vacancy | None
    messages: Sequence[Message]


class SqlConversationRepository(_BaseRepository):
    """Диалоги. Помимо выборок — подсчёт по вакансии для безопасного удаления."""

    """Диалоги. ``get_with_lock`` защищает от двойного сообщения кандидату."""

    async def upsert(self, conversation: Conversation) -> Conversation:
        row = await self._first(
            select(ConversationRow).where(
                ConversationRow.channel == conversation.channel.value,
                ConversationRow.external_id == conversation.external_id,
            )
        )
        if row is None:
            row = await self._session.get(ConversationRow, conversation.id)
        if row is None:
            row = ConversationRow(id=conversation.id)
            mappers.apply_conversation(row, conversation)
            self._session.add(row)
        else:
            mappers.apply_conversation(row, conversation)
            row.updated_at = utcnow()
        await self._session.flush()
        return mappers.conversation_from_row(row)

    async def get(self, conversation_id: UUID) -> Conversation | None:
        row = await self._session.get(ConversationRow, conversation_id)
        return mappers.conversation_from_row(row) if row is not None else None

    async def get_by_external(self, channel: Channel, external_id: str) -> Conversation | None:
        row = await self._first(
            select(ConversationRow).where(
                ConversationRow.channel == channel.value,
                ConversationRow.external_id == external_id,
            )
        )
        return mappers.conversation_from_row(row) if row is not None else None

    async def get_with_lock(self, conversation_id: UUID) -> Conversation | None:
        """Прочитать диалог, заблокировав строку до конца транзакции.

        На PostgreSQL это ``SELECT ... FOR UPDATE``: второй воркер ждёт коммита
        первого и видит уже новое состояние — так исключается двойное сообщение.
        В SQLite блокировок строк нет, поэтому там выполняется обычное чтение;
        сериализацию обеспечивает сама СУБД на уровне записи.
        """
        stmt = select(ConversationRow).where(ConversationRow.id == conversation_id)
        if self._dialect in FOR_UPDATE_DIALECTS:
            stmt = stmt.with_for_update()
        row = await self._first(stmt)
        return mappers.conversation_from_row(row) if row is not None else None

    async def count_for_vacancy(self, vacancy_id: UUID) -> int:
        """Сколько диалогов связано с вакансией."""
        total = await self._session.scalar(
            select(func.count()).select_from(ConversationRow).where(ConversationRow.vacancy_id == vacancy_id)
        )
        return int(total or 0)

    async def list_by_state(
        self,
        states: Sequence[ConversationState],
        *,
        limit: int = 100,
        offset: int = 0,
    ) -> Sequence[Conversation]:
        if not states:
            return []
        limit, offset = _clamp_pagination(limit, offset)
        rows = await self._all(
            select(ConversationRow)
            .where(ConversationRow.state.in_([state.value for state in states]))
            .order_by(
                ConversationRow.next_action_at.is_(None),
                ConversationRow.next_action_at.asc(),
                ConversationRow.updated_at.desc(),
            )
            .limit(limit)
            .offset(offset)
        )
        return [mappers.conversation_from_row(row) for row in rows]

    async def list_with_details(
        self,
        states: Sequence[ConversationState],
        *,
        limit: int = 100,
        offset: int = 0,
        messages_limit: int = 20,
    ) -> Sequence[ConversationDetails]:
        """Диалоги со связями за фиксированное число запросов (3 при любом ``limit``).

        Это единственный способ получить связанные данные без N+1: связи
        подгружаются ``selectinload`` (один дополнительный запрос на связь), а
        сообщения — отдельной выборкой с ограничением, чтобы не вытащить всю
        переписку кандидата за год.
        """
        if not states:
            return []
        limit, offset = _clamp_pagination(limit, offset)
        rows = await self._all(
            select(ConversationRow)
            .where(ConversationRow.state.in_([state.value for state in states]))
            .options(
                selectinload(ConversationRow.candidate),
                selectinload(ConversationRow.vacancy),
            )
            .order_by(ConversationRow.updated_at.desc())
            .limit(limit)
            .offset(offset)
        )
        details: list[ConversationDetails] = []
        for row in rows:
            candidate = (
                mappers.candidate_from_row(row.candidate, self._cipher) if row.candidate is not None else None
            )
            vacancy = mappers.vacancy_from_row(row.vacancy) if row.vacancy is not None else None
            details.append(
                ConversationDetails(
                    conversation=mappers.conversation_from_row(row),
                    candidate=candidate,
                    vacancy=vacancy,
                    messages=[],
                )
            )
        if not details:
            return details
        conversations_by_id = {item.conversation.id: item for item in details}
        message_rows = await self._all(
            select(MessageRow)
            .where(MessageRow.conversation_id.in_(list(conversations_by_id)))
            .order_by(MessageRow.conversation_id, MessageRow.created_at.desc())
            .limit(max(1, messages_limit) * len(conversations_by_id))
        )
        grouped: dict[UUID, list[Message]] = {}
        for message_row in message_rows:
            grouped.setdefault(message_row.conversation_id, []).append(mappers.message_from_row(message_row))
        return [
            ConversationDetails(
                conversation=item.conversation,
                candidate=item.candidate,
                vacancy=item.vacancy,
                messages=tuple(reversed(grouped.get(item.conversation.id, [])))[-messages_limit:],
            )
            for item in details
        ]


# --- Message -----------------------------------------------------------------


class SqlMessageRepository(_BaseRepository):
    """Сообщения диалога."""

    async def add(self, message: Message) -> Message:
        row = await self._session.get(MessageRow, message.id)
        if row is None:
            row = MessageRow(id=message.id)
            mappers.apply_message(row, message)
            self._session.add(row)
        else:
            mappers.apply_message(row, message)
        await self._session.flush()
        return mappers.message_from_row(row)

    async def list_for_conversation(
        self, conversation_id: UUID, *, limit: int = 50, offset: int = 0
    ) -> Sequence[Message]:
        """Последние сообщения диалога в хронологическом порядке."""
        limit, offset = _clamp_pagination(limit, offset)
        rows = await self._all(
            select(MessageRow)
            .where(MessageRow.conversation_id == conversation_id)
            .order_by(MessageRow.created_at.desc(), MessageRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [mappers.message_from_row(row) for row in reversed(rows)]

    async def exists_external_id(self, conversation_id: UUID, external_id: str) -> bool:
        stmt = select(
            exists().where(
                MessageRow.conversation_id == conversation_id,
                MessageRow.external_id == external_id,
            )
        )
        return bool(await self._session.scalar(stmt))


# --- Question answers --------------------------------------------------------


class SqlQuestionAnswerRepository(_BaseRepository):
    """Ответы кандидатов: запись и чтение по диалогу."""

    async def add(self, answer: QuestionAnswer) -> QuestionAnswer:
        row = QuestionAnswerRow(
            id=answer.id,
            conversation_id=answer.conversation_id,
            criterion_key=answer.criterion_key,
            question=answer.question,
            answer=answer.answer,
            score=answer.score,
            is_unclear=answer.is_unclear,
            contradicts_resume=answer.contradicts_resume,
            created_at=answer.created_at,
        )
        self._session.add(row)
        await self._session.flush()
        return answer

    async def list_for_conversation(self, conversation_id: UUID) -> Sequence[QuestionAnswer]:
        rows = await self._all(
            select(QuestionAnswerRow)
            .where(QuestionAnswerRow.conversation_id == conversation_id)
            .order_by(QuestionAnswerRow.created_at)
        )
        return [mappers.question_answer_from_row(row) for row in rows]

    async def latest_for_criterion(self, conversation_id: UUID, criterion_key: str) -> QuestionAnswer | None:
        row = await self._first(
            select(QuestionAnswerRow)
            .where(
                QuestionAnswerRow.conversation_id == conversation_id,
                QuestionAnswerRow.criterion_key == criterion_key,
            )
            .order_by(QuestionAnswerRow.created_at.desc())
            .limit(1)
        )
        return mappers.question_answer_from_row(row) if row is not None else None


# --- Paid actions (бюджет) ---------------------------------------------------


class SqlPaidActionRepository(_BaseRepository):
    """Журнал списаний: запись и сумма за период.

    Сумма считается на стороне БД: при тысячах записей загрузка их в память
    ради сложения — лишняя работа и лишняя память.
    """

    async def add(self, action: PaidAction) -> PaidAction:
        row = PaidActionRow(
            id=action.id,
            company_id=action.company_id,
            channel=action.channel,
            kind=action.kind.value,
            cost=action.cost,
            reference=action.reference,
            created_at=action.created_at or utcnow(),
        )
        self._session.add(row)
        await self._session.flush()
        return action

    async def spent_since(self, company_id: UUID, since: datetime) -> float:
        total = await self._session.scalar(
            select(func.coalesce(func.sum(PaidActionRow.cost), 0.0)).where(
                PaidActionRow.company_id == company_id,
                PaidActionRow.created_at >= since,
            )
        )
        return float(total or 0.0)


# --- Scoring profiles --------------------------------------------------------


class SqlScoringProfileRepository(_BaseRepository):
    """Профили скоринга с критериями и вопросами.

    Критерии и вопросы пишутся целиком при сохранении: профиль — это настройка,
    которую правят как единое целое, а не набор независимых записей.
    """

    async def add(self, profile: ScoringProfile) -> ScoringProfile:
        row = ScoringProfileRow(id=profile.id)
        mappers.apply_profile(row, profile)
        self._session.add(row)
        await self._session.flush()
        await self._replace_children(row.id, profile)
        return await self._reload(row.id)

    async def get(self, profile_id: UUID) -> ScoringProfile | None:
        row = await self._first(select(ScoringProfileRow).where(ScoringProfileRow.id == profile_id))
        if row is None:
            return None
        return await self._reload(row.id)

    async def get_live(self, vacancy_id: UUID) -> ScoringProfile | None:
        row = await self._first(
            select(ScoringProfileRow).where(
                ScoringProfileRow.vacancy_id == vacancy_id,
                ScoringProfileRow.status.in_([ProfileStatus.ACTIVE.value, ProfileStatus.SHADOW.value]),
            )
        )
        return await self._reload(row.id) if row is not None else None

    async def get_latest_version(self, vacancy_id: UUID) -> ScoringProfile | None:
        row = await self._first(
            select(ScoringProfileRow)
            .where(ScoringProfileRow.vacancy_id == vacancy_id)
            .order_by(ScoringProfileRow.version.desc())
            .limit(1)
        )
        return await self._reload(row.id) if row is not None else None

    async def list_versions(self, vacancy_id: UUID, *, limit: int = 50) -> Sequence[ScoringProfile]:
        limit, _ = _clamp_pagination(limit, 0)
        rows = await self._all(
            select(ScoringProfileRow)
            .where(ScoringProfileRow.vacancy_id == vacancy_id)
            .order_by(ScoringProfileRow.version.desc())
            .limit(limit)
        )
        return [await self._reload(row.id) for row in rows]

    async def save(self, profile: ScoringProfile) -> ScoringProfile:
        row = await self._session.get(ScoringProfileRow, profile.id)
        if row is None:
            return await self.add(profile)
        mappers.apply_profile(row, profile)
        await self._session.flush()
        await self._replace_children(row.id, profile)
        return await self._reload(row.id)

    async def publish(self, profile_id: UUID) -> ScoringProfile:
        """Сделать версию живой, архивировав прежнюю.

        Порядок важен: сначала архив прежней живой версии, потом установка
        статуса — иначе частичный уникальный индекс отклонит вторую живую.
        Всё в одной транзакции, поэтому промежуточного состояния не видно.
        """
        row = await self._session.get(ScoringProfileRow, profile_id)
        if row is None:
            raise NotFound(f"Профиль {profile_id} не найден")

        await self._archive_live(row.vacancy_id)
        row.status = ProfileStatus.ACTIVE.value
        row.published_at = utcnow()
        await self._session.flush()
        return await self._reload(row.id)

    async def activate_version(self, vacancy_id: UUID, version: int) -> ScoringProfile:
        """Откат: сделать живой другую версию, архивную не удаляя."""
        row = await self._first(
            select(ScoringProfileRow).where(
                ScoringProfileRow.vacancy_id == vacancy_id,
                ScoringProfileRow.version == version,
            )
        )
        if row is None:
            raise NotFound(f"Версия {version} профиля вакансии {vacancy_id} не найдена")
        return await self.publish(row.id)

    async def _archive_live(self, vacancy_id: UUID) -> None:
        """Снять статус «живой» со всех версий вакансии."""
        rows = await self._all(
            select(ScoringProfileRow).where(
                ScoringProfileRow.vacancy_id == vacancy_id,
                ScoringProfileRow.status.in_([ProfileStatus.ACTIVE.value, ProfileStatus.SHADOW.value]),
            )
        )
        for row in rows:
            row.status = ProfileStatus.ARCHIVED.value
        if rows:
            await self._session.flush()

    async def _replace_children(self, profile_id: UUID, profile: ScoringProfile) -> None:
        """Переписать критерии и вопросы профиля.

        Удаление и вставка, а не дифф: набор маленький, а «изменилось ли» на
        списках даёт лишние ветки и ошибки при перестановке.
        """
        await self._session.execute(
            sa_delete(ProfileCriterionRow).where(ProfileCriterionRow.profile_id == profile_id)
        )
        await self._session.execute(
            sa_delete(ProfileQuestionRow).where(ProfileQuestionRow.profile_id == profile_id)
        )
        for position, criterion in enumerate(profile.criteria):
            self._session.add(
                ProfileCriterionRow(
                    id=uuid4(),
                    profile_id=profile_id,
                    key=criterion.key,
                    title=criterion.title,
                    weight=criterion.weight,
                    must_have=criterion.must_have,
                    evidence_hint=criterion.evidence_hint,
                    expectation=criterion.expectation,
                    source=criterion.source,
                    position=position,
                )
            )
        for position, question in enumerate(profile.questions):
            self._session.add(
                ProfileQuestionRow(
                    id=uuid4(),
                    profile_id=profile_id,
                    criterion_key=question.criterion_key,
                    text=question.text,
                    kind=question.kind.value,
                    informativeness=question.informativeness,
                    is_custom=question.is_custom,
                    position=position,
                )
            )
        await self._session.flush()

    async def _reload(self, profile_id: UUID) -> ScoringProfile:
        row = await self._first(
            select(ScoringProfileRow)
            .where(ScoringProfileRow.id == profile_id)
            .options(
                selectinload(ScoringProfileRow.criteria),
                selectinload(ScoringProfileRow.questions),
            )
        )
        if row is None:
            raise NotFound(f"Профиль {profile_id} не найден")
        return mappers.profile_from_row(row, row.criteria, row.questions)


# --- Message drafts (очередь подтверждения) ----------------------------------


class SqlMessageDraftRepository(_BaseRepository):
    """Черновики сообщений.

    Инвариант «один ожидающий черновик на диалог» держится двумя механизмами:
    вытеснением предыдущего в этой же транзакции и частичным уникальным индексом
    в БД. Второй нужен на случай гонки двух воркеров — вытеснение в коде её
    не закрывает.
    """

    async def add(self, draft: DraftMessage) -> DraftMessage:
        # Вытесняем прежний ожидающий черновик: его решение уже устарело.
        previous = await self._first(
            select(MessageDraftRow).where(
                MessageDraftRow.conversation_id == draft.conversation_id,
                MessageDraftRow.status == DraftStatus.PENDING.value,
            )
        )
        if previous is not None:
            previous.status = DraftStatus.SUPERSEDED.value
            await self._session.flush()

        row = MessageDraftRow(id=draft.id)
        mappers.apply_message_draft(row, draft)
        try:
            async with self._session.begin_nested():
                self._session.add(row)
                await self._session.flush()
        except IntegrityError:
            # Гонка: другой процесс успел вставить ожидающий черновик. Возвращаем
            # актуальный, чтобы вызывающая сторона работала с существующим.
            existing = await self._first(
                select(MessageDraftRow).where(
                    MessageDraftRow.conversation_id == draft.conversation_id,
                    MessageDraftRow.status == DraftStatus.PENDING.value,
                )
            )
            if existing is None:
                raise
            return mappers.message_draft_from_row(existing)
        return mappers.message_draft_from_row(row)

    async def get(self, draft_id: UUID) -> DraftMessage | None:
        row = await self._session.get(MessageDraftRow, draft_id)
        return mappers.message_draft_from_row(row) if row is not None else None

    async def get_pending_for_conversation(self, conversation_id: UUID) -> DraftMessage | None:
        row = await self._first(
            select(MessageDraftRow).where(
                MessageDraftRow.conversation_id == conversation_id,
                MessageDraftRow.status == DraftStatus.PENDING.value,
            )
        )
        return mappers.message_draft_from_row(row) if row is not None else None

    async def list_pending(self, *, limit: int = 100) -> Sequence[DraftMessage]:
        limit, _ = _clamp_pagination(limit, 0)
        rows = await self._all(
            select(MessageDraftRow)
            .where(MessageDraftRow.status == DraftStatus.PENDING.value)
            .order_by(MessageDraftRow.created_at.asc())
            .limit(limit)
        )
        return [mappers.message_draft_from_row(row) for row in rows]

    async def save(self, draft: DraftMessage) -> DraftMessage:
        row = await self._session.get(MessageDraftRow, draft.id)
        if row is None:
            row = MessageDraftRow(id=draft.id)
            mappers.apply_message_draft(row, draft)
            self._session.add(row)
        else:
            mappers.apply_message_draft(row, draft)
        await self._session.flush()
        return mappers.message_draft_from_row(row)


# --- Privacy (152-ФЗ) --------------------------------------------------------


class SqlPrivacyRepository(_BaseRepository):
    """Сроки хранения, экспорт и удаление персональных данных.

    Удаление не ломает воронку: сообщения и резюме удаляются, учётная запись
    кандидата обезличивается, а диалоги и брони остаются как обезличенная
    история.
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
        """Удалить данные, срок хранения которых истёк.

        Порядок важен: сначала резюме (на них ссылаются скрининги), затем
        остальное. Ограничение ``batch_size`` не даёт одной транзакции вырасти
        до размера всей таблицы.
        """
        resumes = await self._delete_older_than(ResumeRow, ResumeRow.fetched_at, resumes_before, batch_size)
        screenings = await self._delete_older_than(
            ScreeningRow, ScreeningRow.created_at, screenings_before, batch_size
        )
        messages = await self._delete_older_than(
            MessageRow, MessageRow.created_at, messages_before, batch_size
        )
        drafts = await self._delete_older_than(
            MessageDraftRow, MessageDraftRow.created_at, drafts_before, batch_size
        )
        audit = await self._delete_older_than(AuditLogRow, AuditLogRow.created_at, audit_before, batch_size)
        await self._session.flush()
        return PurgeStats(
            resumes=resumes,
            messages=messages,
            screenings=screenings,
            drafts=drafts,
            audit=audit,
        )

    async def _delete_older_than(
        self,
        model: Any,
        column: Any,
        cutoff: datetime,
        limit: int,
    ) -> int:
        """Удалить пачку записей старше ``cutoff`` и вернуть их количество."""
        ids = select(model.id).where(column < cutoff).order_by(column.asc()).limit(limit)
        id_list = list((await self._session.scalars(ids)).all())
        if not id_list:
            return 0
        await self._session.execute(sa_delete(model).where(model.id.in_(id_list)))
        return len(id_list)

    async def export_candidate(self, candidate_id: UUID) -> dict[str, Any] | None:
        """Выгрузить все данные кандидата одним словарём (JSON-совместимым)."""
        candidate_row = await self._session.get(CandidateRow, candidate_id)
        if candidate_row is None:
            return None
        candidate = mappers.candidate_from_row(candidate_row, self._cipher)

        conversation_rows = await self._all(
            select(ConversationRow).where(ConversationRow.candidate_id == candidate_id)
        )
        conversation_ids = [row.id for row in conversation_rows]
        message_rows = (
            await self._all(
                select(MessageRow)
                .where(MessageRow.conversation_id.in_(conversation_ids))
                .order_by(MessageRow.created_at.asc())
            )
            if conversation_ids
            else []
        )
        resume_rows = await self._all(select(ResumeRow).where(ResumeRow.candidate_id == candidate_id))
        screening_rows = await self._all(
            select(ScreeningRow).where(ScreeningRow.candidate_id == candidate_id)
        )
        booking_rows = await self._all(select(BookingRow).where(BookingRow.candidate_id == candidate_id))

        return {
            "candidate": {
                "id": str(candidate.id),
                "full_name": candidate.full_name,
                "phone": candidate.phone,
                "email": candidate.email,
                "city": candidate.city,
                "channel": candidate.channel.value,
                "created_at": candidate.created_at.isoformat(),
            },
            "resumes": [
                {
                    "id": str(row.id),
                    "text": mappers.resume_from_row(row, self._cipher).raw_text,
                    "source_url": row.source_url,
                    "fetched_at": mappers.as_utc(row.fetched_at).isoformat(),
                }
                for row in resume_rows
            ],
            "conversations": [
                {
                    "id": str(row.id),
                    "state": row.state,
                    "channel": row.channel,
                    "created_at": mappers.as_utc(row.created_at).isoformat(),
                }
                for row in conversation_rows
            ],
            "messages": [
                {
                    "conversation_id": str(row.conversation_id),
                    "direction": row.direction,
                    "body": row.body,
                    "created_at": mappers.as_utc(row.created_at).isoformat(),
                }
                for row in message_rows
            ],
            "screenings": [
                {
                    "id": str(row.id),
                    "total_score": row.total_score,
                    "verdict": row.verdict,
                    "summary": row.summary,
                    "created_at": mappers.as_utc(row.created_at).isoformat(),
                }
                for row in screening_rows
            ],
            "bookings": [
                {
                    "id": str(row.id),
                    "starts_at": mappers.as_utc(row.starts_at).isoformat(),
                    "status": row.status,
                }
                for row in booking_rows
            ],
            "exported_at": utcnow().isoformat(),
        }

    async def erase_candidate(self, candidate_id: UUID) -> bool:
        """Удалить ПДн кандидата, сохранив обезличенную воронку.

        Удаляются тексты резюме, переписка и результаты скрининга; сама запись
        кандидата обезличивается (имя, телефон, почта). Диалоги и брони остаются:
        без них теряется история работы рекрутера, а ПДн в них уже нет.
        """
        candidate_row = await self._session.get(CandidateRow, candidate_id)
        if candidate_row is None:
            return False

        conversation_ids = list(
            (
                await self._session.scalars(
                    select(ConversationRow.id).where(ConversationRow.candidate_id == candidate_id)
                )
            ).all()
        )
        if conversation_ids:
            await self._session.execute(
                sa_delete(MessageRow).where(MessageRow.conversation_id.in_(conversation_ids))
            )
            await self._session.execute(
                sa_delete(MessageDraftRow).where(MessageDraftRow.conversation_id.in_(conversation_ids))
            )
        await self._session.execute(sa_delete(ScreeningRow).where(ScreeningRow.candidate_id == candidate_id))
        await self._session.execute(sa_delete(ResumeRow).where(ResumeRow.candidate_id == candidate_id))

        # Обезличивание: имя и контакты больше не персональные данные.
        candidate_row.full_name = "Кандидат (данные удалены)"
        candidate_row.phone = None
        candidate_row.phone_index = None
        candidate_row.email = None
        candidate_row.email_index = None
        await self._session.flush()
        return True


# --- Screening ---------------------------------------------------------------


class SqlScreeningRepository(_BaseRepository):
    """Результаты скрининга."""

    async def save(self, result: ScreeningResult) -> ScreeningResult:
        row = await self._session.get(ScreeningRow, result.id)
        if row is None:
            row = ScreeningRow(id=result.id)
            mappers.apply_screening(row, result)
            self._session.add(row)
        else:
            mappers.apply_screening(row, result)
        # Ключ дедупликации скрининга — хэш «текст резюме + вакансия». В самой
        # доменной сущности его нет, поэтому берём из резюме и считаем здесь:
        # иначе get_by_content_hash не нашёл бы ни одной записи.
        # Ключ кэша приходит из сервиса: только он знает версию профиля, по
        # которой посчитан результат. Пересчёт здесь дал бы другой хэш и кэш
        # перестал бы находить записи после смены критериев.
        row.content_hash = result.content_hash or await self._content_hash_for(result)
        await self._session.flush()
        return mappers.screening_from_row(row)

    async def _content_hash_for(self, result: ScreeningResult) -> str | None:
        resume_row = await self._session.get(ResumeRow, result.resume_id)
        if resume_row is None or not resume_row.raw_text:
            return None
        plain_text = self._cipher.decrypt(resume_row.raw_text)
        return Resume.content_hash(plain_text, vacancy_id=result.vacancy_id)

    async def get_latest(self, candidate_id: UUID, vacancy_id: UUID) -> ScreeningResult | None:
        row = await self._first(
            select(ScreeningRow)
            .where(
                ScreeningRow.candidate_id == candidate_id,
                ScreeningRow.vacancy_id == vacancy_id,
            )
            .order_by(ScreeningRow.created_at.desc(), ScreeningRow.id.desc())
            .limit(1)
        )
        return mappers.screening_from_row(row) if row is not None else None

    async def get_by_content_hash(self, content_hash: str) -> ScreeningResult | None:
        row = await self._first(
            select(ScreeningRow)
            .where(ScreeningRow.content_hash == content_hash)
            .order_by(ScreeningRow.created_at.desc(), ScreeningRow.id.desc())
            .limit(1)
        )
        return mappers.screening_from_row(row) if row is not None else None


# --- Booking -----------------------------------------------------------------


class SqlBookingRepository(_BaseRepository):
    """Брони собеседований. Идемпотентность — по ``idempotency_key``."""

    async def save(self, booking: Booking) -> Booking:
        row = await self._session.get(BookingRow, booking.id)
        if row is None and booking.idempotency_key:
            # Ключ уже занят -> действие уже выполнено. Возвращаем существующую
            # бронь: это не ошибка, повтор должен быть безопасным.
            row = await self._find_row_by_idempotency_key(booking.idempotency_key)
            if row is not None and row.id != booking.id:
                logger.warning(
                    "booking_idempotency_hit",
                    idempotency_key=booking.idempotency_key,
                    existing_booking_id=str(row.id),
                    requested_booking_id=str(booking.id),
                )
        if row is None:
            row = BookingRow(id=booking.id)
            mappers.apply_booking(row, booking)
            self._session.add(row)
        else:
            mappers.apply_booking(row, booking)
            row.updated_at = utcnow()
        try:
            await self._session.flush()
        except IntegrityError as exc:
            # Сработало ограничение БД: либо частичный уникальный индекс
            # ``bookings(manager_id, starts_at)`` (активная бронь на это время уже
            # есть), либо гонка по ``idempotency_key``. И то и другое — не сбой
            # БД, а конфликт слота: транслируем в доменную ошибку.
            msg = (
                f"Слот {booking.start.isoformat()} у менеджера {booking.manager_id} "
                "уже занят: сработало ограничение БД"
            )
            raise SlotConflict(msg) from exc
        return mappers.booking_from_row(row)

    async def _find_row_by_idempotency_key(self, key: str) -> BookingRow | None:
        row: BookingRow | None = await self._first(
            select(BookingRow).where(BookingRow.idempotency_key == key)
        )
        return row

    async def get(self, booking_id: UUID) -> Booking | None:
        row = await self._session.get(BookingRow, booking_id)
        return mappers.booking_from_row(row) if row is not None else None

    async def get_by_idempotency_key(self, key: str) -> Booking | None:
        if not key:
            return None
        row = await self._find_row_by_idempotency_key(key)
        return mappers.booking_from_row(row) if row is not None else None

    async def get_for_conversation(self, conversation_id: UUID) -> Booking | None:
        """Последняя бронь по диалогу (карточка кандидата, демо-прогон)."""
        row: BookingRow | None = await self._first(
            select(BookingRow)
            .where(BookingRow.conversation_id == conversation_id)
            .order_by(BookingRow.created_at.desc())
            .limit(1)
        )
        return mappers.booking_from_row(row) if row is not None else None

    async def list_active_for_manager(
        self, manager_id: str, *, start: datetime, end: datetime
    ) -> Sequence[Booking]:
        """Активные брони менеджера, пересекающиеся с окном ``[start, end)``."""
        rows = await self._all(
            select(BookingRow)
            .where(
                BookingRow.manager_id == manager_id,
                BookingRow.status.in_(ACTIVE_BOOKING_STATUSES),
                BookingRow.starts_at < end,
                BookingRow.ends_at > start,
            )
            .order_by(BookingRow.starts_at.asc())
        )
        return [mappers.booking_from_row(row) for row in rows]

    async def find_active_overlap(self, manager_id: str, *, start: datetime, end: datetime) -> Booking | None:
        """Первая активная бронь менеджера, пересекающаяся с окном ``[start, end)``.

        Вызывается внутри транзакции сохранения брони (транзакционный захват
        слота). На PostgreSQL чтение идёт с ``FOR UPDATE``: параллельный воркер
        ждёт коммита и видит уже занятый слот. В SQLite блокировок строк нет, а
        гонку закрывает частичный уникальный индекс ``bookings(manager_id,
        starts_at)`` для активных статусов (нарушение → ``IntegrityError`` →
        ``SlotConflict``).
        """
        stmt = (
            select(BookingRow)
            .where(
                BookingRow.manager_id == manager_id,
                BookingRow.status.in_(ACTIVE_BOOKING_STATUSES),
                BookingRow.starts_at < end,
                BookingRow.ends_at > start,
            )
            .order_by(BookingRow.starts_at.asc())
            .limit(1)
        )
        if self._dialect in FOR_UPDATE_DIALECTS:
            stmt = stmt.with_for_update()
        row = await self._first(stmt)
        return mappers.booking_from_row(row) if row is not None else None


# --- Outbox ------------------------------------------------------------------


class SqlOutboxRepository(_BaseRepository):
    """Транзакционный outbox."""

    async def add(self, entry: OutboxEntry) -> OutboxEntry:
        existing = await self._first(select(OutboxRow).where(OutboxRow.dedupe_key == entry.dedupe_key))
        if existing is not None:
            # Дедупликация на уровне бизнес-ключа: один шаг воронки — одна запись.
            return mappers.outbox_from_row(existing)

        row = OutboxRow(id=entry.id)
        mappers.apply_outbox(row, entry)

        if self._dialect == "sqlite":
            # SQLite: SAVEPOINT здесь вреден. Драйвер ``pysqlite`` не поддерживает
            # вложенные транзакции корректно — ``RELEASE SAVEPOINT`` завершает
            # внешнюю транзакцию, и откат Unit of Work перестаёт отменять запись
            # (именно так терялась атомарность шага воронки).
            # SAVEPOINT нужен только для того, чтобы пойманный ``IntegrityError``
            # не «отравил» транзакцию. В SQLite это не требуется: нарушение
            # ограничения откатывает только сам оператор, а транзакция остаётся
            # рабочей. Гонки двух писателей там тоже нет — писатель один.
            self._session.add(row)
            try:
                await self._session.flush()
            except IntegrityError:
                existing = await self._first(
                    select(OutboxRow).where(OutboxRow.dedupe_key == entry.dedupe_key)
                )
                if existing is None:
                    raise
                return mappers.outbox_from_row(existing)
            return mappers.outbox_from_row(row)

        try:
            # PostgreSQL: SAVEPOINT обязателен. Без него пойманный IntegrityError
            # помечает всю транзакцию как aborted, и откатывается работа всего шага.
            async with self._session.begin_nested():
                self._session.add(row)
        except IntegrityError:
            existing = await self._first(select(OutboxRow).where(OutboxRow.dedupe_key == entry.dedupe_key))
            if existing is None:
                raise
            return mappers.outbox_from_row(existing)
        return mappers.outbox_from_row(row)

    async def claim_batch(self, *, limit: int, now: datetime) -> Sequence[OutboxEntry]:
        """Забрать пачку готовых задач и увеличить ``attempts``.

        Берём и ``PENDING``, и ``FAILED``: пауза между повторами — свойство
        самой записи (``available_at`` отодвигает :meth:`mark`), а не отдельной
        очереди. Иначе упавшая задача не вернулась бы в работу никогда.

        На PostgreSQL используется ``FOR UPDATE SKIP LOCKED``: два воркера берут
        разные строки и не ждут друг друга. В SQLite блокировок строк нет —
        эквивалентом служит обычная выборка (в демо воркер один).
        """
        limit, _ = _clamp_pagination(limit, 0)
        claimable = (
            OutboxStatus.PENDING.value,
            OutboxStatus.FAILED.value,
        )
        stmt = (
            select(OutboxRow)
            .where(OutboxRow.status.in_(claimable), OutboxRow.available_at <= now)
            .order_by(OutboxRow.available_at.asc(), OutboxRow.created_at.asc())
            .limit(limit)
        )
        if self._dialect == "postgresql":
            stmt = stmt.with_for_update(skip_locked=True)
        rows = await self._all(stmt)
        for row in rows:
            row.attempts += 1
        await self._session.flush()
        return [mappers.outbox_from_row(row) for row in rows]

    async def mark(self, entry_id: UUID, status: OutboxStatus, *, error: str | None = None) -> None:
        row = await self._session.get(OutboxRow, entry_id)
        if row is None:
            msg = f"Запись outbox {entry_id} не найдена"
            raise NotFound(msg)
        row.status = status.value
        if error is not None:
            # Полный traceback в БД не нужен: он содержит ПДн и занимает мегабайты.
            row.last_error = error[:MAX_ERROR_LEN]
        if status in {OutboxStatus.PENDING, OutboxStatus.FAILED}:
            # Вернуть задачу в очередь с нарастающей паузой — не бомбить API сразу.
            backoff_s = min(3600.0, 30.0 * (2 ** max(0, row.attempts - 1)))
            row.available_at = datetime.now(tz=UTC) + timedelta(seconds=backoff_s)
        await self._session.flush()


# --- Idempotency -------------------------------------------------------------


class SqlIdempotencyRepository(_BaseRepository):
    """Ключи идемпотентности: захват через ``INSERT ... ON CONFLICT``."""

    async def acquire(self, key: str, *, scope: str, ttl_hours: int = 72) -> bool:
        """Захватить ключ. ``True`` — захватили мы, ``False`` — ключ уже был.

        Истёкший ключ можно захватить повторно (``WHERE expires_at <= now``):
        иначе таблица росла бы вечно, а старые действия блокировали бы новые.
        """
        now = datetime.now(tz=UTC)
        ttl = max(1, ttl_hours)
        expires_at = now + timedelta(hours=ttl)
        values: Mapping[str, Any] = {
            "id": uuid4(),
            "key": key,
            "scope": scope,
            "created_at": now,
            "expires_at": expires_at,
        }
        stmt = self._build_acquire_statement(values, now=now, expires_at=expires_at)
        result = await self._session.execute(stmt)
        cursor = cast("CursorResult[Any]", result)
        inserted = (cursor.rowcount or 0) > 0
        if not inserted:
            logger.debug("idempotency_duplicate", scope=scope)
        return inserted

    def _build_acquire_statement(
        self, values: Mapping[str, Any], *, now: datetime, expires_at: datetime
    ) -> Any:
        """``INSERT ... ON CONFLICT DO UPDATE`` для нужного диалекта.

        ``DO UPDATE`` (а не ``DO NOTHING``) нужен, чтобы можно было перехватить
        **истёкший** ключ: ``WHERE expires_at <= now`` пропускает только старые
        записи, актуальные остаются занятыми.
        """
        dialect = self._dialect
        if dialect == "postgresql":
            return (
                pg_insert(IdempotencyKeyRow)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[IdempotencyKeyRow.key, IdempotencyKeyRow.scope],
                    set_={"created_at": now, "expires_at": expires_at},
                    where=IdempotencyKeyRow.expires_at <= now,
                )
            )
        if dialect == "sqlite":
            return (
                sqlite_insert(IdempotencyKeyRow)
                .values(**values)
                .on_conflict_do_update(
                    index_elements=[IdempotencyKeyRow.key, IdempotencyKeyRow.scope],
                    set_={"created_at": now, "expires_at": expires_at},
                    where=IdempotencyKeyRow.expires_at <= now,
                )
            )
        msg = f"Диалект {dialect!r} не поддерживается: нужен PostgreSQL или SQLite"
        raise NotImplementedError(msg)


# --- Audit log ---------------------------------------------------------------


class SqlAuditLogRepository(_BaseRepository):
    """Журнал действий. Только добавление — намеренно нет ни update, ни delete."""

    async def append(
        self,
        *,
        actor: str,
        action: str,
        entity_type: str,
        entity_id: str | None = None,
        payload: Mapping[str, Any] | None = None,
        correlation_id: str | None = None,
    ) -> UUID:
        """Записать событие. ``payload`` не должен содержать ПДн."""
        entry_id = uuid4()
        self._session.add(
            AuditLogRow(
                id=entry_id,
                actor=actor,
                action=action,
                entity_type=entity_type,
                entity_id=entity_id,
                payload=dict(payload or {}),
                created_at=utcnow(),
                correlation_id=correlation_id,
            )
        )
        await self._session.flush()
        return entry_id

    async def list_for_entity(
        self, entity_type: str, entity_id: str, *, limit: int = 100, offset: int = 0
    ) -> Sequence[dict[str, Any]]:
        limit, offset = _clamp_pagination(limit, offset)
        rows = await self._all(
            select(AuditLogRow)
            .where(AuditLogRow.entity_type == entity_type, AuditLogRow.entity_id == entity_id)
            .order_by(AuditLogRow.created_at.desc(), AuditLogRow.id.desc())
            .limit(limit)
            .offset(offset)
        )
        return [
            {
                "id": str(row.id),
                "actor": row.actor,
                "action": row.action,
                "entity_type": row.entity_type,
                "entity_id": row.entity_id,
                "payload": dict(row.payload or {}),
                "created_at": mappers.as_utc(row.created_at).isoformat(),
                "correlation_id": row.correlation_id,
            }
            for row in rows
        ]


__all__ = [
    "ACTIVE_BOOKING_STATUSES",
    "FOR_UPDATE_DIALECTS",
    "MAX_ERROR_LEN",
    "ConversationDetails",
    "SqlAuditLogRepository",
    "SqlBookingRepository",
    "SqlCandidateRepository",
    "SqlConversationRepository",
    "SqlIdempotencyRepository",
    "SqlMessageRepository",
    "SqlOutboxRepository",
    "SqlResumeRepository",
    "SqlScreeningRepository",
    "SqlVacancyRepository",
]
