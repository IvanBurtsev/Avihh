"""Отображение JSON hh.ru в DTO из ``hirebot.ports.dto``.

Здесь живёт всё знание о форме ответов hh.ru. Маппер намеренно «мягкий»:
любое неожиданное поле трактуется как отсутствующее, потому что API hh.ru
дополняется без предупреждения, а падение обработки отклика из-за нового поля
недопустимо.

Отдельная ответственность — нормализация резюме в плоский текст
(``ResumePayload.text``). Это единственный текст, который уходит в LLM, поэтому:

* **не** включаются пол, возраст, дата рождения и фото — скоринг по защищённым
  признакам запрещён (см. раздел 2.5 ARCHITECTURE.md);
* **не** включаются ФИО и контакты — они передаются отдельными полями DTO,
  и только они редактируются при ``LLM__SEND_PII=false``;
* структура читаемая: заголовки разделов, список мест работы, навыки, образование.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any

from hirebot.adapters.parsing import (
    as_dict,
    as_list,
    dig,
    int_or_zero,
    parse_datetime,
    str_or_empty,
    str_or_none,
)
from hirebot.domain.enums import Channel, MessageDirection
from hirebot.ports.dto import (
    ChannelAction,
    ConversationSnapshot,
    InboundMessage,
    InboundResponse,
    ResumePayload,
    VacancyPayload,
)

__all__ = [
    "parse_actions",
    "parse_conversation",
    "parse_inbound_response",
    "parse_message",
    "parse_resume",
    "parse_vacancy",
    "resume_to_text",
]

#: Статусы переписки hh.ru (``messaging_status``), при которых писать нельзя,
#: с человекочитаемой причиной. Тексты важны: их видит рекрутер в очереди.
_MESSAGING_BLOCKED: Mapping[str, str] = {
    "no_invitation": "переписка доступна только после приглашения",
    "in_a_row_limit": "отправлено 5 сообщений подряд, нужен ответ кандидата",
    "overall_limit": "исчерпан общий лимит сообщений по этому отклику",
    "disabled_by_employer": "переписка отключена работодателем",
    "resume_deleted": "кандидат удалил резюме",
    "archived": "отклик в архиве",
    "prohibited": "переписка запрещена площадкой",
}


def _candidate_name_from_resume(resume: Mapping[str, Any]) -> str:
    parts = [str_or_empty(resume.get("first_name")), str_or_empty(resume.get("last_name"))]
    middle = str_or_empty(resume.get("middle_name"))
    if middle:
        parts.append(middle)
    return " ".join(part for part in parts if part).strip()


def parse_vacancy(item: Mapping[str, Any]) -> VacancyPayload:
    """Активная вакансия работодателя -> ``VacancyPayload``."""
    return VacancyPayload(
        external_id=str_or_empty(item.get("id")),
        title=str_or_empty(item.get("name")) or str_or_empty(item.get("title")),
        description=str_or_empty(item.get("description")),
        city=str_or_none(dig(item, "area", "name")),
        is_active=str_or_empty(item.get("archived")).lower() not in {"true"},
        raw=dict(item),
    )


def parse_inbound_response(
    item: Mapping[str, Any], *, vacancy_external_id: str | None = None
) -> InboundResponse:
    """Элемент коллекции откликов -> ``InboundResponse``.

    ``conversation_external_id`` — идентификатор отклика/приглашения (он же нужен
    для сообщений и действий), ``candidate_external_id`` — идентификатор резюме:
    именно он идентифицирует кандидата внутри площадки.
    """
    negotiation_id = str_or_empty(item.get("id"))
    resume = as_dict(item.get("resume"))
    resume_id = str_or_none(resume.get("id"))
    unread = int_or_zero(dig(item, "counters", "unread_messages"))
    has_updates = bool(item.get("has_updates"))
    return InboundResponse(
        channel=Channel.HH,
        conversation_external_id=negotiation_id,
        candidate_external_id=resume_id or negotiation_id,
        vacancy_external_id=(vacancy_external_id or str_or_empty(dig(item, "vacancy", "id")) or ""),
        candidate_name=_candidate_name_from_resume(resume),
        resume_external_id=resume_id,
        resume_url=str_or_none(resume.get("url")),
        received_at=parse_datetime(item.get("created_at")),
        is_new=bool(has_updates or unread or not item),
        cover_letter="",  # сопроводительное письмо в hh.ru приходит сообщением, а не полем отклика
        raw=dict(item),
    )


def parse_actions(item: Mapping[str, Any]) -> tuple[ChannelAction, ...]:
    """``actions[]`` отклика -> ``ChannelAction``.

    Ключевое архитектурное требование: URL и метод действия берутся только
    из ответа сервера (раздел 2.2 ARCHITECTURE.md), никакого хардкода путей.
    Описание аргументов (``arguments[]``) сохраняем в ``extra_args``: состав
    аргументов «может отличаться для каждого отклика», и адаптер обязан
    заполнять именно то, что требует сервер.
    """
    actions: list[ChannelAction] = []
    for raw_action in as_list(item.get("actions")):
        action = as_dict(raw_action)
        key = str_or_empty(action.get("id"))
        url = str_or_empty(action.get("url"))
        if not key or not url:
            continue
        arguments: dict[str, Any] = {}
        for raw_argument in as_list(action.get("arguments")):
            argument = as_dict(raw_argument)
            argument_id = str_or_empty(argument.get("id"))
            if argument_id:
                arguments[argument_id] = bool(argument.get("required"))
        actions.append(
            ChannelAction(
                key=key,
                title=str_or_empty(action.get("name")) or key,
                url=url,
                method=(str_or_empty(action.get("method")) or "PUT").upper(),
                requires_message=bool(arguments.get("message")),
                extra_args={
                    "arguments": arguments,
                    "enabled": bool(action.get("enabled", True)),
                    "resulting_employer_state": str_or_none(action.get("resulting_employer_state")),
                },
            )
        )
    return tuple(actions)


def parse_conversation(raw: Mapping[str, Any]) -> ConversationSnapshot:
    """Полный отклик -> ``ConversationSnapshot`` (доступность переписки и действия)."""
    status = str_or_none(raw.get("messaging_status"))
    if status is None:
        available = True
        reason: str | None = None
    elif status == "ok":
        available = True
        reason = None
    else:
        available = False
        reason = _MESSAGING_BLOCKED.get(status, f"переписка недоступна ({status})")
    return ConversationSnapshot(
        external_id=str_or_empty(raw.get("id")),
        actions=parse_actions(raw),
        unread_count=int_or_zero(dig(raw, "counters", "unread_messages")),
        messaging_available=available,
        reason_unavailable=reason,
    )


def parse_message(item: Mapping[str, Any]) -> InboundMessage:
    """Элемент ``items[]`` переписки -> ``InboundMessage``.

    Автор определяется по ``author.participant_type``: ``applicant`` — кандидат,
    всё остальное (``employer``, отсутствие поля) считаем исходящим.
    """
    from hirebot.adapters.parsing import parse_datetime

    participant = str_or_empty(dig(item, "author", "participant_type")).lower()
    direction = MessageDirection.INBOUND if participant == "applicant" else MessageDirection.OUTBOUND
    created = parse_datetime(item.get("created_at"))
    return InboundMessage(
        external_id=str_or_empty(item.get("id")),
        direction=direction,
        text=str_or_empty(item.get("text")),
        created_at=created if created is not None else _now(),
        author_name=str_or_empty(dig(item, "author", "name")),
    )


def _now() -> datetime:
    """Момент чтения. Нужен только как fallback, когда площадка не отдала время."""
    return datetime.now(tz=UTC)


def parse_resume(raw: Mapping[str, Any], *, external_id: str, source_url: str | None = None) -> ResumePayload:
    """Полное резюме -> ``ResumePayload``.

    ``text`` — нормализованный плоский текст без защищённых признаков и без ФИО;
    ``raw`` сохраняем целиком (аудит и переразбор), в промпты он не попадает.
    """
    hidden = [str_or_empty(name) for name in as_list(raw.get("hidden_fields"))]
    parsed: dict[str, Any] = {
        "total_experience_months": int_or_zero(dig(raw, "total_experience", "months")),
        "education_level": str_or_none(dig(raw, "education", "level", "name")),
        "area": str_or_none(dig(raw, "area", "name")),
        "salary": as_dict(raw.get("salary")),
        "skills": [str_or_empty(skill) for skill in as_list(raw.get("skill_set"))],
        "positions": [
            str_or_empty(as_dict(experience).get("position")) for experience in as_list(raw.get("experience"))
        ],
        "languages": [
            str_or_empty(as_dict(language).get("name")) for language in as_list(raw.get("language"))
        ],
        "professional_roles": [
            str_or_empty(as_dict(role).get("name")) for role in as_list(raw.get("professional_roles"))
        ],
        "hidden_fields": hidden,
    }
    contacts = _contacts(raw)
    return ResumePayload(
        external_id=external_id or str_or_empty(raw.get("id")),
        text=resume_to_text(raw),
        candidate_name=_candidate_name_from_resume(raw),
        contacts=contacts,
        parsed=parsed,
        source_url=source_url,
        unavailable_reason=("часть данных скрыта кандидатом: " + ", ".join(hidden) if hidden else None),
    )


def _contacts(raw: Mapping[str, Any]) -> dict[str, str]:
    contacts: dict[str, str] = {}
    for raw_contact in as_list(raw.get("contact")):
        contact = as_dict(raw_contact)
        kind = str_or_empty(contact.get("type")).lower()
        value = str_or_empty(contact.get("value"))
        if value and kind in {"cell", "phone", "home", "work", "email", "skype"}:
            contacts.setdefault(kind, value)
    for key in ("phone", "email"):
        direct = str_or_none(raw.get(key))
        if direct:
            contacts.setdefault(key, direct)
    return contacts


def _format_period(experience: Mapping[str, Any]) -> str:
    start = str_or_empty(experience.get("start"))
    end = str_or_empty(experience.get("end"))
    start_text = start[:7] if len(start) >= 7 else start
    end_text = (end[:7] if len(end) >= 7 else end) or "по настоящее время"
    if not start_text:
        return end_text if end else ""
    return f"{start_text} — {end_text}"


def _experience_lines(raw: Mapping[str, Any]) -> list[str]:
    lines: list[str] = []
    for raw_experience in as_list(raw.get("experience")):
        experience = as_dict(raw_experience)
        position = str_or_empty(experience.get("position"))
        company = str_or_empty(experience.get("company"))
        head = " — ".join(part for part in (position, company) if part)
        period = _format_period(experience)
        if period:
            head = f"{head} ({period})" if head else period
        if head:
            lines.append(f"- {head}")
        description = str_or_empty(experience.get("description"))
        if description:
            lines.append(f"  {description}")
    return lines


def _education_text(raw: Mapping[str, Any]) -> str:
    education = as_dict(raw.get("education"))
    parts: list[str] = []
    level = str_or_none(dig(education, "level", "name"))
    if level:
        parts.append(level)
    for raw_item in as_list(education.get("primary")):
        item = as_dict(raw_item)
        name = str_or_empty(item.get("name"))
        organization = str_or_empty(item.get("organization"))
        year = str_or_empty(item.get("year"))
        chunk = ", ".join(part for part in (name, organization, year) if part)
        if chunk:
            parts.append(chunk)
    return "; ".join(parts)


def resume_to_text(raw: Mapping[str, Any]) -> str:
    """Нормализовать резюме в плоский текст для LLM.

    Текст детерминирован: один и тот же JSON даёт одну и ту же строку, а значит
    ``content_hash`` (и кэш ответов модели) работает корректно.
    """
    if not raw:
        return ""
    lines: list[str] = []
    title = str_or_none(raw.get("title"))
    if title:
        lines.append(f"Должность: {title}")
    area = str_or_none(dig(raw, "area", "name"))
    if area:
        lines.append(f"Город: {area}")
    months = int_or_zero(dig(raw, "total_experience", "months"))
    if months:
        years, rest = divmod(months, 12)
        parts = ([f"{years} г."] if years else []) + ([f"{rest} мес."] if rest else [])
        lines.append(f"Общий опыт: {' '.join(parts)}")
    salary = as_dict(raw.get("salary"))
    amount = salary.get("amount")
    if amount:
        currency = str_or_empty(salary.get("currency")) or ""
        lines.append(f"Желаемая зарплата: {amount} {currency}".strip())
    employment = str_or_none(raw.get("employment"))
    schedule = str_or_none(raw.get("schedule"))
    if employment or schedule:
        lines.append(f"Занятость: {' / '.join(p for p in (employment, schedule) if p)}")

    experience_lines = _experience_lines(raw)
    if experience_lines:
        lines.append("Опыт работы:")
        lines.extend(experience_lines)

    skills = [str_or_empty(skill) for skill in as_list(raw.get("skill_set"))]
    skills = [skill for skill in skills if skill]
    if skills:
        lines.append(f"Ключевые навыки: {', '.join(skills)}")

    about = str_or_none(raw.get("skills"))
    if about:
        lines.append(f"О себе: {about}")

    education = _education_text(raw)
    if education:
        lines.append(f"Образование: {education}")

    languages: list[str] = []
    for raw_language in as_list(raw.get("language")):
        language = as_dict(raw_language)
        name = str_or_empty(language.get("name"))
        level = str_or_empty(dig(language, "level", "name"))
        chunk = ": ".join(part for part in (name, level) if part)
        if chunk:
            languages.append(chunk)
    if languages:
        lines.append(f"Языки: {', '.join(languages)}")

    roles = [str_or_empty(as_dict(role).get("name")) for role in as_list(raw.get("professional_roles"))]
    roles = [role for role in roles if role]
    if roles:
        lines.append(f"Профессиональные роли: {', '.join(roles)}")

    certificates = [str_or_empty(as_dict(item).get("title")) for item in as_list(raw.get("certificate"))]
    certificates = [certificate for certificate in certificates if certificate]
    if certificates:
        lines.append(f"Курсы и сертификаты: {', '.join(certificates)}")

    return "\n".join(lines).strip()
