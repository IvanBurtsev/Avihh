"""DTO для передачи данных между адаптерами площадок и доменом.

Это «сырые» структуры внешнего мира. Они намеренно повторяют форму ответов
площадок и не смешиваются с доменными сущностями: маппинг делает адаптер.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

from hirebot.domain.enums import Channel, MessageDirection


@dataclass(slots=True, frozen=True)
class VacancyPayload:
    """Вакансия, как её отдаёт площадка."""

    external_id: str
    title: str
    description: str = ""
    city: str | None = None
    is_active: bool = True
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ResumePayload:
    """Резюме за откликом.

    ``text`` — нормализованный плоский текст: именно он уходит в LLM.
    ``raw`` не используется в промптах, но нужен для аудита и переразбора.
    """

    external_id: str
    text: str
    candidate_name: str = ""
    contacts: dict[str, str] = field(default_factory=dict)
    parsed: dict[str, Any] = field(default_factory=dict)
    source_url: str | None = None
    unavailable_reason: str | None = None


@dataclass(slots=True, frozen=True)
class InboundResponse:
    """Свежий отклик кандидата (вход воронки)."""

    channel: Channel
    conversation_external_id: str
    candidate_external_id: str
    vacancy_external_id: str
    candidate_name: str = ""
    resume_external_id: str | None = None
    resume_url: str | None = None
    received_at: datetime | None = None
    is_new: bool = True
    cover_letter: str = ""
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class InboundMessage:
    """Сообщение из чата площадки."""

    external_id: str
    direction: MessageDirection
    text: str
    created_at: datetime
    author_name: str = ""
    attachments: tuple[str, ...] = ()


@dataclass(slots=True, frozen=True)
class SentMessage:
    """Подтверждение отправки.

    ``external_id`` обязателен: без него невозможно сопоставить отправленное
    сообщение с реальностью и корректно решать конфликты при ретраях.
    """

    external_id: str | None
    sent_at: datetime


@dataclass(slots=True, frozen=True)
class ChannelAction:
    """Действие, которое площадка разрешает по этому диалогу.

    Для hh.ru действия приходят гиперссылками в ``actions[]``, и их пути
    нельзя хардкодить — только выполнять то, что вернул сервер. Это защищает
    интеграцию от изменений на стороне площадки.
    """

    key: str
    title: str
    url: str
    method: str = "PUT"
    requires_message: bool = False
    extra_args: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True, frozen=True)
class ConversationSnapshot:
    """Текущее состояние диалога на площадке."""

    external_id: str
    actions: tuple[ChannelAction, ...] = ()
    unread_count: int = 0
    messaging_available: bool = True
    reason_unavailable: str | None = None


@dataclass(slots=True, frozen=True)
class TokenBundle:
    """Пара токенов OAuth с абсолютным временем истечения.

    Абсолютное время, а не ``expires_in``: относительные значения теряют смысл
    при сохранении в БД и приводят к «протухшим» токенам после рестарта.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    token_type: str = "Bearer"
