"""Маппинг ORM-строк в доменные сущности и обратно.

Один модуль на все сущности — чтобы правило «где шифруются ПДн» было в одном
месте и не разъезжалось между репозиториями. Домен не знает ни про SQLAlchemy,
ни про шифрование: он получает и отдаёт открытые значения.

Отдельная тонкость — время. SQLite не хранит ``tzinfo``: значение возвращается
наивным. Домен же требует timezone-aware UTC (иначе слоты собеседований
съезжают). Поэтому на чтении все ``datetime`` проходят через :func:`as_utc`.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from hirebot.domain.entities import (
    Booking,
    Candidate,
    Company,
    Conversation,
    DraftMessage,
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
    DraftStatus,
    MessageDirection,
    MessageKind,
    OutboxStatus,
    ScreeningVerdict,
)
from hirebot.infra.db.models import (
    BookingRow,
    CandidateRow,
    CompanyRow,
    ConversationRow,
    MessageDraftRow,
    MessageRow,
    OutboxRow,
    ResumeRow,
    ScreeningRow,
    VacancyRow,
)
from hirebot.infra.security.crypto import FieldCipher

#: Поля, которые действительно лежат в JSON-колонке ``screenings.criteria``.
_CRITERION_KEYS = ("key", "title", "weight", "score", "must_have", "evidence", "is_gap")


def as_utc(value: datetime) -> datetime:
    """Привести время к aware-UTC (SQLite теряет зону)."""
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def as_utc_optional(value: datetime | None) -> datetime | None:
    return None if value is None else as_utc(value)


def _str_list(raw: Any) -> list[str]:
    """Привести JSON-значение к списку строк, не падая на неожиданных данных."""
    if not isinstance(raw, list):
        return []
    return [str(item) for item in raw]


def criterion_to_dict(criterion: ScreeningCriterion) -> dict[str, Any]:
    """Сериализовать критерий для JSON-колонки."""
    return {
        "key": criterion.key,
        "title": criterion.title,
        "weight": criterion.weight,
        "score": criterion.score,
        "must_have": criterion.must_have,
        "evidence": criterion.evidence,
        "is_gap": criterion.is_gap,
    }


def criterion_from_dict(raw: Mapping[str, Any]) -> ScreeningCriterion:
    """Восстановить критерий. Неизвестные поля игнорируются (устойчивость к миграции)."""
    return ScreeningCriterion(
        key=str(raw.get("key", "")),
        title=str(raw.get("title", "")),
        weight=float(raw.get("weight", 1.0)),
        score=float(raw.get("score", 0.0)),
        must_have=bool(raw.get("must_have", False)),
        evidence=str(raw.get("evidence", "")),
        is_gap=bool(raw.get("is_gap", False)),
    )


def criteria_from_json(raw: Any) -> list[ScreeningCriterion]:
    if not isinstance(raw, list):
        return []
    return [criterion_from_dict(item) for item in raw if isinstance(item, Mapping)]


# --- Vacancy -----------------------------------------------------------------


def company_from_row(row: CompanyRow) -> Company:
    return Company(
        id=row.id,
        name=row.name,
        slug=row.slug,
        created_at=as_utc(row.created_at),
    )


def apply_company(row: CompanyRow, entity: Company) -> None:
    """Перенести доменные поля компании в строку."""
    row.name = entity.name
    row.slug = entity.slug
    row.created_at = entity.created_at


def vacancy_from_row(row: VacancyRow) -> Vacancy:
    return Vacancy(
        id=row.id,
        channel=Channel(row.channel),
        external_id=row.external_id,
        title=row.title,
        company_id=row.company_id,
        description=row.description,
        city=row.city,
        is_active=row.is_active,
        criteria=[dict(item) for item in (row.criteria or []) if isinstance(item, Mapping)],
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def apply_vacancy(row: VacancyRow, entity: Vacancy) -> None:
    """Перенести доменные поля в строку. ``id`` не трогаем: его задаёт репозиторий."""
    row.company_id = entity.company_id
    row.channel = entity.channel.value
    row.external_id = entity.external_id
    row.title = entity.title
    row.description = entity.description
    row.city = entity.city
    row.is_active = entity.is_active
    row.criteria = [dict(item) for item in entity.criteria]
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


# --- Candidate ---------------------------------------------------------------


def candidate_from_row(row: CandidateRow, cipher: FieldCipher) -> Candidate:
    return Candidate(
        id=row.id,
        channel=Channel(row.channel),
        external_id=row.external_id,
        full_name=row.full_name,
        company_id=row.company_id,
        phone=cipher.decrypt_optional(row.phone),
        email=cipher.decrypt_optional(row.email),
        city=row.city,
        created_at=as_utc(row.created_at),
    )


def apply_candidate(row: CandidateRow, entity: Candidate, cipher: FieldCipher) -> None:
    """Записать кандидата: контакты — только шифротекстом, плюс blind index."""
    row.company_id = entity.company_id
    row.channel = entity.channel.value
    row.external_id = entity.external_id
    row.full_name = entity.full_name
    row.city = entity.city
    row.created_at = entity.created_at
    row.phone = cipher.encrypt_optional(entity.phone)
    row.email = cipher.encrypt_optional(entity.email)
    # Пустой индекс не нужен: NULL не участвует в поиске и не мешает уникальности.
    row.phone_index = cipher.blind_index(entity.phone) if entity.phone else None
    row.email_index = cipher.blind_index(entity.email) if entity.email else None


# --- Resume ------------------------------------------------------------------


def resume_from_row(row: ResumeRow, cipher: FieldCipher) -> Resume:
    return Resume(
        id=row.id,
        candidate_id=row.candidate_id,
        channel=Channel(row.channel),
        external_id=row.external_id,
        raw_text=cipher.decrypt(row.raw_text),
        parsed=dict(row.parsed or {}),
        source_url=row.source_url,
        fetched_at=as_utc(row.fetched_at),
    )


def resume_content_hash(entity: Resume) -> str:
    """Хэш нормализованного текста резюме (ключ кэша LLM)."""
    return Resume.content_hash(entity.raw_text)


def apply_resume(row: ResumeRow, entity: Resume, cipher: FieldCipher) -> None:
    row.candidate_id = entity.candidate_id
    row.channel = entity.channel.value
    row.external_id = entity.external_id
    row.raw_text = cipher.encrypt(entity.raw_text)
    row.parsed = dict(entity.parsed)
    row.source_url = entity.source_url
    row.content_hash = resume_content_hash(entity)
    row.fetched_at = entity.fetched_at


# --- Conversation ------------------------------------------------------------


def conversation_from_row(row: ConversationRow) -> Conversation:
    return Conversation(
        id=row.id,
        company_id=row.company_id,
        candidate_id=row.candidate_id,
        vacancy_id=row.vacancy_id,
        channel=Channel(row.channel),
        external_id=row.external_id,
        state=ConversationState(row.state),
        auto_messages_sent=row.auto_messages_sent,
        last_inbound_at=as_utc_optional(row.last_inbound_at),
        last_outbound_at=as_utc_optional(row.last_outbound_at),
        next_action_at=as_utc_optional(row.next_action_at),
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def apply_conversation(row: ConversationRow, entity: Conversation) -> None:
    row.company_id = entity.company_id
    row.candidate_id = entity.candidate_id
    row.vacancy_id = entity.vacancy_id
    row.channel = entity.channel.value
    row.external_id = entity.external_id
    row.state = entity.state.value
    row.auto_messages_sent = entity.auto_messages_sent
    row.last_inbound_at = entity.last_inbound_at
    row.last_outbound_at = entity.last_outbound_at
    row.next_action_at = entity.next_action_at
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


# --- Message -----------------------------------------------------------------


def message_from_row(row: MessageRow) -> Message:
    return Message(
        id=row.id,
        conversation_id=row.conversation_id,
        direction=MessageDirection(row.direction),
        body=row.body,
        kind=MessageKind(row.kind),
        external_id=row.external_id,
        generated_by_llm=row.generated_by_llm,
        model=row.model,
        prompt_version=row.prompt_version,
        created_at=as_utc(row.created_at),
        sent_at=as_utc_optional(row.sent_at),
    )


def apply_message(row: MessageRow, entity: Message) -> None:
    row.conversation_id = entity.conversation_id
    row.direction = entity.direction.value
    row.body = entity.body
    row.kind = entity.kind.value
    row.external_id = entity.external_id
    row.generated_by_llm = entity.generated_by_llm
    row.model = entity.model
    row.prompt_version = entity.prompt_version
    row.created_at = entity.created_at
    row.sent_at = entity.sent_at


# --- Screening ---------------------------------------------------------------


def message_draft_from_row(row: MessageDraftRow) -> DraftMessage:
    return DraftMessage(
        id=row.id,
        conversation_id=row.conversation_id,
        body=row.body,
        kind=MessageKind(row.kind),
        target_state=ConversationState(row.target_state),
        status=DraftStatus(row.status),
        reason=row.reason or "",
        model=row.model,
        prompt_version=row.prompt_version,
        generated_by_llm=bool(row.generated_by_llm),
        created_at=as_utc(row.created_at),
        reviewed_at=as_utc_optional(row.reviewed_at),
        reviewer=row.reviewer,
        review_comment=row.review_comment,
    )


def apply_message_draft(row: MessageDraftRow, entity: DraftMessage) -> None:
    """Перенести поля черновика в строку (``id`` задаёт репозиторий)."""
    row.conversation_id = entity.conversation_id
    row.body = entity.body
    row.kind = entity.kind.value
    row.target_state = entity.target_state.value
    row.status = entity.status.value
    row.reason = entity.reason
    row.model = entity.model
    row.prompt_version = entity.prompt_version
    row.generated_by_llm = entity.generated_by_llm
    row.created_at = entity.created_at
    row.reviewed_at = entity.reviewed_at
    row.reviewer = entity.reviewer
    row.review_comment = entity.review_comment


def screening_from_row(row: ScreeningRow) -> ScreeningResult:
    return ScreeningResult(
        id=row.id,
        resume_id=row.resume_id,
        vacancy_id=row.vacancy_id,
        candidate_id=row.candidate_id,
        total_score=row.total_score,
        verdict=ScreeningVerdict(row.verdict),
        criteria=criteria_from_json(row.criteria),
        summary=row.summary,
        gaps=_str_list(row.gaps),
        strengths=_str_list(row.strengths),
        questions=_str_list(row.questions),
        model=row.model,
        prompt_version=row.prompt_version,
        cache_hit=row.cache_hit,
        created_at=as_utc(row.created_at),
    )


def apply_screening(row: ScreeningRow, entity: ScreeningResult) -> None:
    row.resume_id = entity.resume_id
    row.vacancy_id = entity.vacancy_id
    row.candidate_id = entity.candidate_id
    row.total_score = entity.total_score
    row.verdict = entity.verdict.value
    row.criteria = [criterion_to_dict(item) for item in entity.criteria]
    row.summary = entity.summary
    row.gaps = list(entity.gaps)
    row.strengths = list(entity.strengths)
    row.questions = list(entity.questions)
    row.model = entity.model
    row.prompt_version = entity.prompt_version
    row.cache_hit = entity.cache_hit
    row.created_at = entity.created_at


# --- Booking -----------------------------------------------------------------


def booking_from_row(row: BookingRow) -> Booking:
    return Booking(
        id=row.id,
        conversation_id=row.conversation_id,
        candidate_id=row.candidate_id,
        vacancy_id=row.vacancy_id,
        manager_id=row.manager_id,
        start=as_utc(row.starts_at),
        end=as_utc(row.ends_at),
        timezone=row.timezone,
        status=BookingStatus(row.status),
        external_meeting_id=row.external_meeting_id,
        external_event_id=row.external_event_id,
        idempotency_key=row.idempotency_key or "",
        created_at=as_utc(row.created_at),
        updated_at=as_utc(row.updated_at),
    )


def apply_booking(row: BookingRow, entity: Booking) -> None:
    row.conversation_id = entity.conversation_id
    row.candidate_id = entity.candidate_id
    row.vacancy_id = entity.vacancy_id
    row.manager_id = entity.manager_id
    row.starts_at = entity.start
    row.ends_at = entity.end
    row.timezone = entity.timezone
    row.status = entity.status.value
    row.external_meeting_id = entity.external_meeting_id
    row.external_event_id = entity.external_event_id
    # Пустой ключ пишем как NULL: иначе UNIQUE не дал бы создать вторую бронь
    # без ключа (все пустые строки считались бы дублями).
    row.idempotency_key = entity.idempotency_key or None
    row.created_at = entity.created_at
    row.updated_at = entity.updated_at


# --- Outbox ------------------------------------------------------------------


def outbox_from_row(row: OutboxRow) -> OutboxEntry:
    return OutboxEntry(
        id=row.id,
        topic=row.topic,
        payload=dict(row.payload or {}),
        dedupe_key=row.dedupe_key,
        status=OutboxStatus(row.status),
        attempts=row.attempts,
        available_at=as_utc(row.available_at),
        last_error=row.last_error,
        created_at=as_utc(row.created_at),
    )


def apply_outbox(row: OutboxRow, entity: OutboxEntry) -> None:
    row.topic = entity.topic
    row.payload = dict(entity.payload)
    row.dedupe_key = entity.dedupe_key
    row.status = entity.status.value
    row.attempts = entity.attempts
    row.available_at = entity.available_at
    row.last_error = entity.last_error
    row.created_at = entity.created_at


__all__ = [
    "apply_booking",
    "apply_candidate",
    "apply_company",
    "apply_conversation",
    "apply_message",
    "apply_message_draft",
    "apply_outbox",
    "apply_resume",
    "apply_screening",
    "apply_vacancy",
    "as_utc",
    "as_utc_optional",
    "booking_from_row",
    "candidate_from_row",
    "company_from_row",
    "conversation_from_row",
    "criteria_from_json",
    "criterion_from_dict",
    "criterion_to_dict",
    "message_draft_from_row",
    "message_from_row",
    "outbox_from_row",
    "resume_content_hash",
    "resume_from_row",
    "screening_from_row",
    "vacancy_from_row",
]
