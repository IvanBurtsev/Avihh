"""Порт канала (площадки): HH.ru, Avito и любые будущие.

Ключевое архитектурное решение: сервисы воронки не знают, откуда пришёл
кандидат. Они работают только через этот протокол. Добавление новой площадки
= новый адаптер, без правок в скрининге, диалоге и расписании.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Protocol, runtime_checkable

from hirebot.domain.entities import InterviewSlot
from hirebot.domain.enums import Channel
from hirebot.ports.dto import (
    ConversationSnapshot,
    InboundMessage,
    InboundResponse,
    ResumePayload,
    SentMessage,
    VacancyPayload,
)


@runtime_checkable
class ChannelPort(Protocol):
    """Единый интерфейс площадки.

    Каждый метод обязан быть идемпотентным по чтению и безопасным к ретраям
    по записи: повторный вызов ``send_message`` с тем же ``idempotency_key``
    не должен создать второе сообщение.
    """

    channel: Channel

    async def healthcheck(self) -> bool:
        """Доступна ли площадка с текущими учётными данными."""
        ...

    async def list_vacancies(self) -> Sequence[VacancyPayload]:
        """Активные вакансии работодателя."""
        ...

    async def fetch_new_responses(
        self, *, vacancy_external_id: str | None = None, limit: int = 50
    ) -> Sequence[InboundResponse]:
        """Свежие отклики, которые мы ещё не обрабатывали.

        Реализация обязана вернуть только новые/непрочитанные элементы, чтобы
        поллинг не порождал повторную обработку одних и тех же кандидатов.
        """
        ...

    async def fetch_resume(self, resume_external_id: str) -> ResumePayload:
        """Полное резюме.

        При отсутствии прав/квоты бросает ``ResumeAccessDenied``: это штатная
        ситуация, и она не должна валить весь цикл обработки.
        """
        ...

    async def get_conversation(self, conversation_external_id: str) -> ConversationSnapshot:
        """Состояние диалога: доступна ли переписка и какие действия разрешены."""
        ...

    async def list_messages(
        self, conversation_external_id: str, *, since: datetime | None = None
    ) -> Sequence[InboundMessage]:
        """История переписки."""
        ...

    async def send_message(
        self,
        conversation_external_id: str,
        text: str,
        *,
        idempotency_key: str,
        kind_hint: str | None = None,
    ) -> SentMessage:
        """Отправить сообщение в чат площадки.

        ``idempotency_key`` обязателен и пробрасывается в БД: он защищает от
        дублей при ретраях, таймаутах и падениях воркера между отправкой и
        фиксацией результата.
        """
        ...

    async def perform_action(
        self,
        conversation_external_id: str,
        action_key: str,
        *,
        message: str | None = None,
        idempotency_key: str,
        extra_args: dict[str, str] | None = None,
    ) -> None:
        """Выполнить действие площадки по ключу (приглашение, отказ, перенос).

        Реализация ищет действие в ``actions[]``, полученных от площадки, и не
        конструирует URL самостоятельно.
        """
        ...


@runtime_checkable
class CalendarPort(Protocol):
    """Календарь собеседований.

    Реализуется адаптером Talantix (GraphQL). Интерфейс намеренно узкий:
    свободных слотов как таковых площадка не отдаёт, поэтому сервис строит
    сетку сам, а календарь сообщает только занятость.
    """

    async def list_busy(
        self, *, manager_ids: Sequence[str], start: datetime, end: datetime
    ) -> Sequence[object]:
        """Занятые интервалы участников в окне [start, end)."""
        ...

    async def find_free_slots(
        self,
        *,
        manager_ids: Sequence[str],
        start: datetime,
        end: datetime,
        duration_minutes: int,
        limit: int = 3,
    ) -> Sequence[InterviewSlot]:
        """Подобрать слоты с учётом занятости, буферов и рабочих часов."""
        ...

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
        """Создать встречу. Возвращает объект с ``external_id``."""
        ...

    async def cancel_meeting(self, external_id: str) -> None:
        """Отменить встречу."""
        ...

    async def get_meeting(self, external_id: str) -> object | None:
        """Перечитать встречу — обязательный шаг верификации после создания."""
        ...
