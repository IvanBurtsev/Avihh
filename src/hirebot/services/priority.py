"""Приоритизация кандидатов: кому писать первым.

Зачем отдельный сервис, а не сортировка «по проценту».

Простой процент не отвечает на вопрос «кого обрабатывать сейчас», потому что:

* **квоты площадки ограничены** (у hh.ru — 500 просмотров резюме в день), и
  тратить их на слабых нельзя;
* **кандидат ждёт**: отклик часовой давности и суточной — разные ситуации;
* **данные могут быть неполными**: 78% по четырём критериям и по шести —
  разные кандидаты.

Поэтому приоритет — это **взвешенная сумма объяснимых вкладов**, а не
«магическая» поправка. Коэффициенты задаются, и в карточке видно, из чего
сложилось число.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from uuid import UUID

from hirebot.domain.entities import Candidate, Conversation, ScreeningResult, Vacancy
from hirebot.domain.enums import ConversationState
from hirebot.domain.scoring import (
    DEFAULT_PRIORITY_WEIGHTS,
    PriorityBreakdown,
    availability_score,
    freshness_score,
)
from hirebot.logging_setup import get_logger
from hirebot.services.base import ServiceBase

logger = get_logger(__name__)

#: Сколько кандидатов отдавать в очередь за один раз.
DEFAULT_QUEUE_LIMIT = 50

#: Состояния, в которых кандидат ещё «в работе» и попадает в очередь.
QUEUE_STATES: frozenset[ConversationState] = frozenset(
    {
        ConversationState.NEW,
        ConversationState.RESUME_FETCHED,
        ConversationState.SCREENED,
        ConversationState.QUESTIONS_SENT,
        ConversationState.AWAITING_ANSWER,
        ConversationState.AWAITING_SLOT_CHOICE,
        ConversationState.HUMAN_REVIEW,
    }
)


@dataclass(slots=True)
class QueueItem:
    """Кандидат в очереди с объяснением приоритета."""

    conversation_id: UUID
    candidate_name: str
    state: ConversationState
    percent: float
    coverage: float
    priority: float
    blockers: tuple[str, ...] = ()
    age_hours: float | None = None
    contributions: dict[str, float] = field(default_factory=dict)
    vacancy_title: str = ""

    @property
    def needs_human(self) -> bool:
        return bool(self.blockers) or self.coverage < 0.7


class PriorityService(ServiceBase):
    """Расчёт приоритета и очередь обработки."""

    def breakdown_for(
        self,
        *,
        screening: ScreeningResult | None,
        received_at: datetime | None = None,
        ready_days: int | None = None,
        weights: dict[str, float] | None = None,
    ) -> PriorityBreakdown:
        """Посчитать приоритет по результату скрининга.

        Свежесть считается от момента отклика; при отсутствии данных о
        готовности выйти используется нейтральное значение — неизвестность не
        должна наказывать кандидата.
        """
        resolved_weights = dict(weights or DEFAULT_PRIORITY_WEIGHTS)
        now = self.now()
        age_hours = 0.0
        if received_at is not None:
            age_hours = max(0.0, (now - received_at).total_seconds() / 3600.0)

        percent = screening.match_percent if screening is not None else 0.0
        coverage = screening.confidence if screening is not None else 0.0
        return PriorityBreakdown(
            percent=percent,
            coverage=coverage,
            freshness=freshness_score(age_hours),
            availability=availability_score(ready_days=ready_days),
            weights=resolved_weights,
            has_blocker=bool(screening.blockers) if screening is not None else False,
        )

    async def queue(
        self,
        *,
        company_id: UUID,
        vacancy_id: UUID | None = None,
        limit: int = DEFAULT_QUEUE_LIMIT,
        states: frozenset[ConversationState] | None = None,
    ) -> list[QueueItem]:
        """Очередь кандидатов, отсортированная по приоритету.

        Сортировка выполняется по рассчитанному приоритету, а не по дате
        отклика: иначе слабый отклик, пришедший первым, обрабатывается раньше
        сильного, пришедшего следом.

        Кандидаты без скрининга попадают в очередь с процентом 0: их всё равно
        нужно обработать, но после оценённых.
        """
        async with self.uow() as uow:
            conversations = list(
                await uow.conversations.list_by_state(list(states or QUEUE_STATES), limit=max(limit * 4, 200))
            )
            items: list[QueueItem] = []
            for conversation in conversations:
                if conversation.company_id != company_id:
                    continue
                if vacancy_id is not None and conversation.vacancy_id != vacancy_id:
                    continue
                item = await self._build_item(uow, conversation)
                if item is not None:
                    items.append(item)

        items.sort(key=lambda item: (-item.priority, item.conversation_id))
        if len(items) > limit:
            logger.info("priority_queue_truncated", total=len(items), limit=limit)
        return items[:limit]

    async def _build_item(self, uow: object, conversation: Conversation) -> QueueItem | None:
        """Собрать элемент очереди: кандидат, скрининг, приоритет."""
        from hirebot.ports.repositories import UnitOfWork

        if not isinstance(uow, UnitOfWork):
            msg = "Ожидался UnitOfWork"
            raise TypeError(msg)
        candidate: Candidate | None = await uow.candidates.get(conversation.candidate_id)
        vacancy: Vacancy | None = await uow.vacancies.get(conversation.vacancy_id)
        screening = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
        breakdown = self.breakdown_for(
            screening=screening,
            received_at=conversation.created_at,
        )
        return QueueItem(
            conversation_id=conversation.id,
            candidate_name=candidate.full_name if candidate is not None else "Без имени",
            state=conversation.state,
            percent=breakdown.percent,
            coverage=breakdown.coverage,
            priority=breakdown.score,
            blockers=tuple(screening.blockers) if screening is not None else (),
            age_hours=self._age_hours(conversation.created_at),
            contributions=breakdown.contributions(),
            vacancy_title=vacancy.title if vacancy is not None else "",
        )

    def _age_hours(self, moment: datetime) -> float:
        return max(0.0, (self.now() - moment).total_seconds() / 3600.0)


__all__ = ["DEFAULT_QUEUE_LIMIT", "QUEUE_STATES", "PriorityService", "QueueItem"]
