"""Порт LLM.

Модель вызывается только через этот протокол, и только структурированными
запросами с фиксированной схемой ответа. Свободный текст от модели в бизнес-логику
не попадает: любой ответ сначала валидируется Pydantic-схемой.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from hirebot.domain.entities import CandidateReply, ScreeningCriterion


@dataclass(slots=True, frozen=True)
class ScreeningRequest:
    """Запрос на скоринг резюме под вакансию."""

    vacancy_title: str
    vacancy_description: str
    criteria: Sequence[dict[str, Any]]
    resume_text: str
    # Хэш содержимого: ключ кэша и одновременно защита от повторной оплаты.
    content_hash: str
    prompt_version: str = "v1"


@dataclass(slots=True, frozen=True)
class ScreeningDraft:
    """Черновик результата скрининга от модели (до сохранения).

    ``model`` — провенанс: имя модели, которая дала оценки. Без него в аудите
    оказывается модель из настроек, и по истории невозможно ответить, чем именно
    был принят спорный вердикт (fake-заглушка или боевая LLM).
    """

    total_score: float
    summary: str = ""
    criteria: Sequence[ScreeningCriterion] = field(default_factory=tuple)
    gaps: Sequence[str] = field(default_factory=tuple)
    strengths: Sequence[str] = field(default_factory=tuple)
    questions: Sequence[str] = field(default_factory=tuple)
    model: str = "n/a"


@dataclass(slots=True, frozen=True)
class MessageRequest:
    """Запрос на генерацию сообщения кандидату."""

    kind: str
    candidate_name: str
    vacancy_title: str
    company_name: str
    context: dict[str, Any] = field(default_factory=dict)
    history: Sequence[dict[str, str]] = field(default_factory=tuple)
    # Жёсткие рамки: длина и запрещённые формулировки.
    max_chars: int = 900
    tone: str = "friendly_professional"


@dataclass(slots=True, frozen=True)
class MessageDraft:
    text: str
    model: str = "n/a"
    prompt_version: str = "v1"
    cached: bool = False


@dataclass(slots=True, frozen=True)
class ReplyAnalysisRequest:
    """Запрос на разбор ответа кандидата."""

    candidate_name: str
    reply_text: str
    question_asked: str = ""
    proposed_slots: Sequence[str] = field(default_factory=tuple)


@dataclass(slots=True, frozen=True)
class AnswerAssessmentRequest:
    """Запрос на оценку ответа кандидата по конкретному критерию.

    ``expectation`` и ``evidence_hint`` обязательны: без них модель не знает,
    какой уровень считать закрытым и что считать доказательством.
    """

    criterion_key: str
    criterion_title: str
    expectation: str
    evidence_hint: str
    question: str
    answer: str
    #: Текст резюме для проверки противоречий (может быть пустым).
    resume_text: str = ""


@dataclass(slots=True, frozen=True)
class AnswerAssessment:
    """Оценка ответа по критерию.

    ``is_unclear`` — ответ не содержит информации (уклончивый, «не знаю»).
    ``contradicts_resume`` — ответ противоречит резюме: такой случай уходит
    человеку, а не повышает балл автоматически.
    """

    score: float
    evidence: str = ""
    is_unclear: bool = False
    contradicts_resume: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 100.0:
            msg = f"Оценка ответа вне диапазона 0..100: {self.score}"
            raise ValueError(msg)


@runtime_checkable
class LlmPort(Protocol):
    """Провайдер модели.

    Реализации: ``FakeLlmAdapter`` (детерминированная, без сети — для демо и
    тестов) и ``OpenAiCompatAdapter`` (любой OpenAI-совместимый эндпоинт).
    """

    async def screen_resume(self, request: ScreeningRequest) -> ScreeningDraft:
        """Оценить резюме по критериям вакансии и предложить уточняющие вопросы."""
        ...

    async def compose_message(self, request: MessageRequest) -> MessageDraft:
        """Составить человекоподобное сообщение кандидату."""
        ...

    async def analyze_reply(self, request: ReplyAnalysisRequest) -> CandidateReply:
        """Понять, что ответил кандидат, и выбрать следующий шаг."""
        ...

    async def assess_answer(self, request: AnswerAssessmentRequest) -> AnswerAssessment:
        """Оценить ответ кандидата по критерию профиля.

        Нужно для пересчёта соответствия после уточняющих вопросов: ответ —
        это новые данные, и они должны влиять на решение, а не только на
        переписку.
        """
        ...
