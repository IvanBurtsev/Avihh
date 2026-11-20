"""Порты (контракты) приложения.

Слои зависят только от этих протоколов, а не от конкретных SDK и драйверов.
Направление зависимостей: ``adapters`` -> ``ports`` <- ``services``.
"""

from hirebot.ports.channels import CalendarPort, ChannelPort
from hirebot.ports.dto import (
    ChannelAction,
    ConversationSnapshot,
    InboundMessage,
    InboundResponse,
    ResumePayload,
    SentMessage,
    TokenBundle,
    VacancyPayload,
)
from hirebot.ports.infra import (
    CachePort,
    ClockPort,
    HttpClientPort,
    HttpResponse,
    JobQueue,
    LockPort,
    NullCache,
    RateLimiterPort,
    UnitOfWorkFactory,
)
from hirebot.ports.llm import (
    LlmPort,
    MessageDraft,
    MessageRequest,
    ReplyAnalysisRequest,
    ScreeningDraft,
    ScreeningRequest,
)
from hirebot.ports.repositories import UnitOfWork

__all__ = [
    "CachePort",
    "CalendarPort",
    "ChannelAction",
    "ChannelPort",
    "ClockPort",
    "ConversationSnapshot",
    "HttpClientPort",
    "HttpResponse",
    "InboundMessage",
    "InboundResponse",
    "JobQueue",
    "LlmPort",
    "LockPort",
    "MessageDraft",
    "MessageRequest",
    "NullCache",
    "RateLimiterPort",
    "ReplyAnalysisRequest",
    "ResumePayload",
    "ScreeningDraft",
    "ScreeningRequest",
    "SentMessage",
    "TokenBundle",
    "UnitOfWork",
    "UnitOfWorkFactory",
    "VacancyPayload",
]
