"""ORM-модели: отображение домена на PostgreSQL/SQLite.

Принципы, заложенные здесь:

* **Явный ``naming_convention``.** Без него SQLAlchemy отдаёт Alembic
  неименованные constraint-ы, а PostgreSQL не умеет их менять: миграция
  «удалить unique» становится невыполнимой. Имена нужны заранее.
* **ПДн шифруются на границе репозитория.** Колонки ``candidates.phone``,
  ``candidates.email`` и ``resumes.raw_text`` содержат **шифротекст**; открытые
  значения видит только домен. Для поиска рядом лежат blind index-ы
  (``phone_index``/``email_index``) — детерминированный HMAC.
* **Один и тот же код для PostgreSQL и SQLite.** В продакшене PostgreSQL
  (``JSONB``, ``FOR UPDATE SKIP LOCKED``), в демо и тестах — SQLite
  (``JSON``, без блокировок строк).
* **Связи не подгружаются лениво.** У всех relationship стоит ``lazy="raise"``:
  случайное обращение к связи в async-коде даст понятную ошибку, а не
  ``MissingGreenlet`` в середине обработки отклика. Загружать связи нужно явно
  через ``selectinload`` (см. ``repositories.py``).
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from hirebot.domain.enums import BookingStatus

#: Соглашение об именах constraint-ов и индексов (обязательно для Alembic).
NAMING_CONVENTION: dict[str, str] = {
    "ix": "ix_%(table_name)s_%(column_0_N_name)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}

#: Статусы брони, которые считаются активными, в виде SQL-литералов для
#: частичного уникального индекса ``bookings``: он действует только на активные
#: брони, отменённые и завершённые времени не занимают. Значения берём из
#: перечисления, чтобы индекс и запросы репозитория не разъехались.
_ACTIVE_STATUSES_SQL = ", ".join(
    f"'{status.value}'" for status in (BookingStatus.PROPOSED, BookingStatus.CONFIRMED)
)


def utcnow() -> datetime:
    """Время по умолчанию для колонок. Только timezone-aware UTC."""
    return datetime.now(tz=UTC)


def json_type() -> JSON:
    """JSON-колонка: ``JSONB`` на PostgreSQL, ``JSON`` на SQLite.

    На PostgreSQL берём ``JSONB``: он индексируется GIN и не хранит дубликаты
    ключей. На SQLite ``JSONB`` не существует, поэтому нужен вариант.
    """
    return JSON().with_variant(JSONB(), "postgresql")


class Base(DeclarativeBase):
    """Базовый класс моделей с общим ``MetaData`` и соглашением об именах."""

    metadata = MetaData(naming_convention=NAMING_CONVENTION)


class CompanyRow(Base):
    """Компания-арендатор. Основа мультитенантности."""

    __tablename__ = "companies"

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(120), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)

    channel_accounts: Mapped[list[ChannelAccountRow]] = relationship(back_populates="company", lazy="raise")
    vacancies: Mapped[list[VacancyRow]] = relationship(back_populates="company", lazy="raise")


class ChannelAccountRow(Base):
    """Учётная запись площадки: токены (зашифрованы), статус, лимиты.

    Токены хранятся только здесь и только в зашифрованном виде: логин/пароль
    пользователя не хранятся никогда (п. 3.6 условий API hh.ru).
    """

    __tablename__ = "channel_accounts"
    __table_args__ = (
        UniqueConstraint(
            "company_id",
            "channel",
            "external_account_id",
            name="uq_channel_accounts_company_channel_account",
        ),
        Index("ix_channel_accounts_company_channel", "company_id", "channel"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_account_id: Mapped[str | None] = mapped_column(String(128))
    # Шифротекст (Fernet). Открытые значения в БД не попадают.
    access_token: Mapped[str | None] = mapped_column(Text)
    refresh_token: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    company: Mapped[CompanyRow] = relationship(back_populates="channel_accounts", lazy="raise")


class VacancyRow(Base):
    """Вакансия на площадке. Дедупликация — ``(channel, external_id)``."""

    __tablename__ = "vacancies"
    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_vacancies_channel_external_id"),
        Index("ix_vacancies_company_active", "company_id", "is_active"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    title: Mapped[str] = mapped_column(String(512), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    city: Mapped[str | None] = mapped_column(String(128))
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)
    # Критерии скрининга у каждой вакансии свои -> JSON, а не отдельная таблица.
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(json_type(), nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    company: Mapped[CompanyRow] = relationship(back_populates="vacancies", lazy="raise")


class CandidateRow(Base):
    """Кандидат.

    ``phone``/``email`` — шифротекст; ``phone_index``/``email_index`` — blind index
    (HMAC от нормализованного значения) для поиска по контакту.
    """

    __tablename__ = "candidates"
    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_candidates_channel_external_id"),
        Index("ix_candidates_phone_index", "phone_index"),
        Index("ix_candidates_email_index", "email_index"),
        Index("ix_candidates_company", "company_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    full_name: Mapped[str] = mapped_column(String(512), nullable=False)
    phone: Mapped[str | None] = mapped_column(Text)
    email: Mapped[str | None] = mapped_column(Text)
    phone_index: Mapped[str | None] = mapped_column(String(64))
    email_index: Mapped[str | None] = mapped_column(String(64))
    city: Mapped[str | None] = mapped_column(String(128))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ResumeRow(Base):
    """Резюме. ``raw_text`` — шифротекст (это ПДн в чистом виде)."""

    __tablename__ = "resumes"
    __table_args__ = (
        Index("ix_resumes_candidate_fetched", "candidate_id", "fetched_at"),
        Index("ix_resumes_content_hash", "content_hash"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    raw_text: Mapped[str] = mapped_column(Text, nullable=False, default="")
    parsed: Mapped[dict[str, Any]] = mapped_column(json_type(), nullable=False, default=dict)
    source_url: Mapped[str | None] = mapped_column(String(2048))
    # Хэш нормализованного текста: ключ кэша LLM и ключ дедупликации скрининга.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    fetched_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class ConversationRow(Base):
    """Диалог «кандидат × вакансия» на площадке."""

    __tablename__ = "conversations"
    __table_args__ = (
        UniqueConstraint("channel", "external_id", name="uq_conversations_channel_external_id"),
        # Составной индекс: воркер выбирает «кому пора действовать» именно так.
        Index("ix_conversations_state_next_action_at", "state", "next_action_at"),
        Index("ix_conversations_candidate", "candidate_id"),
        Index("ix_conversations_vacancy", "vacancy_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    vacancy_id: Mapped[UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str] = mapped_column(String(128), nullable=False)
    state: Mapped[str] = mapped_column(String(32), nullable=False)
    auto_messages_sent: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    last_inbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_outbound_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_action_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )

    messages: Mapped[list[MessageRow]] = relationship(back_populates="conversation", lazy="raise")
    drafts: Mapped[list[MessageDraftRow]] = relationship(back_populates="conversation", lazy="raise")
    candidate: Mapped[CandidateRow] = relationship(lazy="raise")
    vacancy: Mapped[VacancyRow] = relationship(lazy="raise")


class MessageRow(Base):
    """Сообщение в диалоге (входящее или исходящее)."""

    __tablename__ = "messages"
    __table_args__ = (
        UniqueConstraint("conversation_id", "external_id", name="uq_messages_conversation_external_id"),
        Index("ix_messages_conversation_created", "conversation_id", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    direction: Mapped[str] = mapped_column(String(16), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    external_id: Mapped[str | None] = mapped_column(String(128))
    generated_by_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    conversation: Mapped[ConversationRow] = relationship(back_populates="messages", lazy="raise")


class MessageDraftRow(Base):
    """Черновик сообщения для очереди подтверждения.

    Частичный уникальный индекс гарантирует инвариант «один ожидающий черновик
    на диалог» на уровне БД, а не только в коде: иначе два параллельных шага
    воронки создали бы два черновика, и рекрутер увидел бы устаревший.
    """

    __tablename__ = "message_drafts"
    __table_args__ = (
        Index(
            "uq_message_drafts_conversation_pending",
            "conversation_id",
            unique=True,
            sqlite_where=text("status = 'pending'"),
            postgresql_where=text("status = 'pending'"),
        ),
        Index("ix_message_drafts_status_created", "status", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    body: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    target_state: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    model: Mapped[str | None] = mapped_column(String(128))
    prompt_version: Mapped[str | None] = mapped_column(String(32))
    generated_by_llm: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    reviewer: Mapped[str | None] = mapped_column(String(120))
    review_comment: Mapped[str | None] = mapped_column(Text)

    conversation: Mapped[ConversationRow] = relationship(back_populates="drafts", lazy="raise")


class ScoringProfileRow(Base):
    """Версия профиля скоринга вакансии.

    Правки копятся в черновике и применяются публикацией: уже обработанные
    кандидаты остаются посчитанными по своей версии. Без этого нельзя объяснить
    прошлые решения — критерии к тому времени уже другие.
    """

    __tablename__ = "scoring_profiles"
    __table_args__ = (
        UniqueConstraint("vacancy_id", "version", name="uq_scoring_profiles_vacancy_version"),
        # Одна «живая» версия на вакансию: одновременно активная и shadow-версия
        # applied to candidates превратили бы решение в лотерею.
        Index(
            "uq_scoring_profiles_vacancy_live",
            "vacancy_id",
            unique=True,
            sqlite_where=text("status IN ('active', 'shadow')"),
            postgresql_where=text("status IN ('active', 'shadow')"),
        ),
        Index("ix_scoring_profiles_vacancy_status", "vacancy_id", "status"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    vacancy_id: Mapped[UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="draft")

    invite_at: Mapped[float] = mapped_column(Float, nullable=False, default=85.0)
    human_at: Mapped[float] = mapped_column(Float, nullable=False, default=55.0)
    reject_at: Mapped[float] = mapped_column(Float, nullable=False, default=40.0)
    threshold_mode: Mapped[str] = mapped_column(String(16), nullable=False, default="absolute")
    top_k: Mapped[int] = mapped_column(Integer, nullable=False, default=10)
    max_questions: Mapped[int] = mapped_column(Integer, nullable=False, default=5)
    auto_invite: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    min_coverage_for_auto: Mapped[float] = mapped_column(Float, nullable=False, default=0.7)

    created_by: Mapped[str] = mapped_column(String(120), nullable=False, default="system")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")

    criteria: Mapped[list[ProfileCriterionRow]] = relationship(
        back_populates="profile", lazy="raise", cascade="all, delete-orphan"
    )
    questions: Mapped[list[ProfileQuestionRow]] = relationship(
        back_populates="profile", lazy="raise", cascade="all, delete-orphan"
    )


class ProfileCriterionRow(Base):
    """Критерий внутри профиля скоринга."""

    __tablename__ = "profile_criteria"
    __table_args__ = (
        UniqueConstraint("profile_id", "key", name="uq_profile_criteria_profile_key"),
        Index("ix_profile_criteria_profile_position", "profile_id", "position"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_profiles.id", ondelete="CASCADE"), nullable=False
    )
    key: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    weight: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    must_have: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    evidence_hint: Mapped[str] = mapped_column(Text, nullable=False, default="")
    expectation: Mapped[str] = mapped_column(Text, nullable=False, default="")
    source: Mapped[str] = mapped_column(String(16), nullable=False, default="catalog")
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    profile: Mapped[ScoringProfileRow] = relationship(back_populates="criteria", lazy="raise")


class ProfileQuestionRow(Base):
    """Вопрос профиля, привязанный к критерию."""

    __tablename__ = "profile_questions"
    __table_args__ = (Index("ix_profile_questions_profile_position", "profile_id", "position"),)

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    profile_id: Mapped[UUID] = mapped_column(
        ForeignKey("scoring_profiles.id", ondelete="CASCADE"), nullable=False
    )
    criterion_key: Mapped[str] = mapped_column(String(64), nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    kind: Mapped[str] = mapped_column(String(16), nullable=False, default="fact")
    informativeness: Mapped[int] = mapped_column(Integer, nullable=False, default=2)
    is_custom: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)

    profile: Mapped[ScoringProfileRow] = relationship(back_populates="questions", lazy="raise")


class ScreeningRow(Base):
    """Результат скоринга резюме под вакансию.

    ``model`` и ``prompt_version`` обязательны: без них невозможно ответить на
    вопрос «почему вчера бот отклонил хорошего кандидата».
    """

    __tablename__ = "screenings"
    __table_args__ = (
        Index("ix_screenings_candidate_vacancy_created", "candidate_id", "vacancy_id", "created_at"),
        Index("ix_screenings_content_hash", "content_hash"),
        Index("ix_screenings_resume", "resume_id"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    resume_id: Mapped[UUID] = mapped_column(ForeignKey("resumes.id", ondelete="CASCADE"), nullable=False)
    vacancy_id: Mapped[UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False)
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    total_score: Mapped[float] = mapped_column(Float, nullable=False)
    verdict: Mapped[str] = mapped_column(String(32), nullable=False)
    #: Процент соответствия (совпадает с total_score, вынесен явно для чтения).
    percent: Mapped[float | None] = mapped_column(Float)
    #: Уверенность: какая доля веса критериев подтверждена данными (0…1).
    coverage: Mapped[float | None] = mapped_column(Float)
    #: Провалы обязательных критериев: исключают автоматическое приглашение.
    blockers: Mapped[list[str]] = mapped_column(json_type(), nullable=False, default=list)
    #: Версия профиля скоринга, по которой принято решение.
    profile_version: Mapped[int | None] = mapped_column(Integer)
    criteria: Mapped[list[dict[str, Any]]] = mapped_column(json_type(), nullable=False, default=list)
    summary: Mapped[str] = mapped_column(Text, nullable=False, default="")
    gaps: Mapped[list[str]] = mapped_column(json_type(), nullable=False, default=list)
    strengths: Mapped[list[str]] = mapped_column(json_type(), nullable=False, default=list)
    questions: Mapped[list[str]] = mapped_column(json_type(), nullable=False, default=list)
    model: Mapped[str] = mapped_column(String(128), nullable=False, default="n/a")
    prompt_version: Mapped[str] = mapped_column(String(32), nullable=False, default="v1")
    cache_hit: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    # Заполняется из resumes.content_hash: по нему ищется готовый скрининг.
    content_hash: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class PaidActionRow(Base):
    """Журнал платных действий: сколько и за что списано.

    Журнал, а не только счётчик: без него нельзя ответить на вопрос «за что
    ушла квота» — а именно этот вопрос задают, когда приходит счёт.
    """

    __tablename__ = "paid_actions"
    __table_args__ = (
        Index("ix_paid_actions_company_created", "company_id", "created_at"),
        Index("ix_paid_actions_company_kind", "company_id", "kind"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    company_id: Mapped[UUID] = mapped_column(ForeignKey("companies.id", ondelete="CASCADE"), nullable=False)
    channel: Mapped[str] = mapped_column(String(16), nullable=False)
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    cost: Mapped[float] = mapped_column(Float, nullable=False, default=1.0)
    reference: Mapped[str] = mapped_column(String(255), nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class BookingRow(Base):
    """Бронь собеседования.

    ``idempotency_key`` уникален: повторная попытка создать встречу при ретрае
    не должна привести ко второй встрече в календаре. Пустая строка хранится как
    ``NULL`` — иначе уникальный индекс не позволил бы больше одной брони без ключа.
    Колонки времени названы ``starts_at``/``ends_at``: ``end`` — зарезервированное
    слово SQL, и кавычить его в каждом запросе unnecessary risk.
    """

    __tablename__ = "bookings"
    __table_args__ = (
        UniqueConstraint("idempotency_key", name="uq_bookings_idempotency_key"),
        Index("ix_bookings_manager_window", "manager_id", "starts_at", "ends_at"),
        Index("ix_bookings_candidate", "candidate_id"),
        Index("ix_bookings_conversation", "conversation_id"),
        # Гарантия на уровне БД: у одного менеджера не может быть двух активных
        # броней на одно и то же время. Площадка конфликты не валидирует, а
        # проверка «прочитал — создал» гонку не закрывает, поэтому последний
        # рубеж — частичный уникальный индекс. Частичные индексы поддерживают и
        # PostgreSQL (``postgresql_where``), и SQLite (``sqlite_where``).
        Index(
            "uq_bookings_manager_active_slot",
            "manager_id",
            "starts_at",
            unique=True,
            postgresql_where=text(f"status IN ({_ACTIVE_STATUSES_SQL})"),
            sqlite_where=text(f"status IN ({_ACTIVE_STATUSES_SQL})"),
        ),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    conversation_id: Mapped[UUID] = mapped_column(
        ForeignKey("conversations.id", ondelete="CASCADE"), nullable=False
    )
    candidate_id: Mapped[UUID] = mapped_column(
        ForeignKey("candidates.id", ondelete="CASCADE"), nullable=False
    )
    vacancy_id: Mapped[UUID] = mapped_column(ForeignKey("vacancies.id", ondelete="CASCADE"), nullable=False)
    manager_id: Mapped[str] = mapped_column(String(128), nullable=False)
    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    ends_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    timezone: Mapped[str] = mapped_column(String(64), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    external_meeting_id: Mapped[str | None] = mapped_column(String(128))
    external_event_id: Mapped[str | None] = mapped_column(String(128))
    idempotency_key: Mapped[str | None] = mapped_column(String(255))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, default=utcnow, onupdate=utcnow
    )


class OutboxRow(Base):
    """Транзакционный outbox: «изменить состояние + запланировать доставку».

    ``available_at`` — время, раньше которого задачу брать нельзя (backoff после
    ошибки). ``attempts`` увеличивается в момент захвата, а не после падения:
    иначе упавший воркер оставлял бы задачу с нулевым счётчиком навсегда.
    """

    __tablename__ = "outbox"
    __table_args__ = (
        UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
        Index("ix_outbox_status_available_at", "status", "available_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    topic: Mapped[str] = mapped_column(String(128), nullable=False)
    payload: Mapped[dict[str, Any]] = mapped_column(json_type(), nullable=False, default=dict)
    dedupe_key: Mapped[str] = mapped_column(String(255), nullable=False)
    status: Mapped[str] = mapped_column(String(16), nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)


class AuditLogRow(Base):
    """Журнал действий: append-only (требование 152-ФЗ и разбор инцидентов).

    Обновление и удаление записей не предусмотрены: история решений по людям
    должна быть неизменяемой. В ``payload`` не попадают ПДн.
    """

    __tablename__ = "audit_log"
    __table_args__ = (
        Index("ix_audit_log_entity", "entity_type", "entity_id"),
        Index("ix_audit_log_created_at", "created_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    actor: Mapped[str] = mapped_column(String(128), nullable=False)
    action: Mapped[str] = mapped_column(String(128), nullable=False)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False)
    entity_id: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict[str, Any]] = mapped_column(json_type(), nullable=False, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    correlation_id: Mapped[str | None] = mapped_column(String(64))


class IdempotencyKeyRow(Base):
    """Ключ идемпотентности: ``UNIQUE(key, scope)``.

    Строка живёт ``ttl_hours``; после истечения её можно захватить повторно
    (``ON CONFLICT DO UPDATE ... WHERE expires_at <= now``), иначе таблица росла
    бы вечно.
    """

    __tablename__ = "idempotency_keys"
    __table_args__ = (
        UniqueConstraint("key", "scope", name="uq_idempotency_keys_key_scope"),
        Index("ix_idempotency_keys_expires_at", "expires_at"),
    )

    id: Mapped[UUID] = mapped_column(Uuid, primary_key=True, default=uuid4)
    key: Mapped[str] = mapped_column(String(255), nullable=False)
    scope: Mapped[str] = mapped_column(String(64), nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False, default=utcnow)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


__all__ = [
    "NAMING_CONVENTION",
    "AuditLogRow",
    "Base",
    "BookingRow",
    "CandidateRow",
    "ChannelAccountRow",
    "CompanyRow",
    "ConversationRow",
    "IdempotencyKeyRow",
    "MessageRow",
    "OutboxRow",
    "ResumeRow",
    "ScreeningRow",
    "VacancyRow",
    "json_type",
    "utcnow",
]
