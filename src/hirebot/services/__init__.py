"""Сервисный слой: бизнес-логика воронки.

Публичный API заморожен: эти имена вызывают API-слой и воркер.

* ``IngestService``       — опрос площадок и приём откликов;
* ``ScreeningService``    — скоринг резюме с пересчётом балла на нашей стороне;
* ``SchedulingService``   — слоты и бронь с верификацией встречи;
* ``ConversationService`` — шаг воронки: скрининг → диалог → приглашение → бронь;
* ``PolicyGuard``         — ограничения автономности бота;
* ``OutboxDispatcher``    — доставка задач транзакционного outbox.
"""

from hirebot.services.conversation import (
    ConversationOutcome,
    ConversationService,
)
from hirebot.services.ingest import IngestOutcome, IngestService, PollResult
from hirebot.services.outbox import OutboxDispatcher
from hirebot.services.policy import PolicyGuard
from hirebot.services.scheduling import SchedulingService
from hirebot.services.screening import ScreeningService
from hirebot.services.topics import (
    BOOK_INTERVIEW_TOPIC,
    DISPATCH_OUTBOX_TOPIC,
    JOB_NAMES,
    POLL_CHANNEL_TOPIC,
    PROCESS_CONVERSATION_TOPIC,
    SEND_MESSAGE_TOPIC,
)

__all__ = [
    "BOOK_INTERVIEW_TOPIC",
    "DISPATCH_OUTBOX_TOPIC",
    "JOB_NAMES",
    "POLL_CHANNEL_TOPIC",
    "PROCESS_CONVERSATION_TOPIC",
    "SEND_MESSAGE_TOPIC",
    "ConversationOutcome",
    "ConversationService",
    "IngestOutcome",
    "IngestService",
    "OutboxDispatcher",
    "PolicyGuard",
    "PollResult",
    "SchedulingService",
    "ScreeningService",
]
