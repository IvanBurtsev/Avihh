"""Pydantic-схемы ответов модели.

Принцип: свободный текст модели в бизнес-логику не попадает. Любой ответ
провалидирован схемой, и только потом превращается в DTO. Невалидный ответ —
это ``LlmError``, а не «как-нибудь разберёмся».

Две важные особенности схем:

* числовые оценки **нормализуются** (``0..100``, ``0..1``), а не отвергаются:
  модель может вернуть ``120`` или ``-5``, и падать из-за этого нет смысла;
* итоговый балл модели игнорируется как поле: он пересчитывается на нашей
  стороне по весам критериев (``extra="ignore"`` отбрасывает лишнее).
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "CriterionScoreSchema",
    "MessageResponseSchema",
    "ReplyAnalysisResponseSchema",
    "ScreeningResponseSchema",
]

Intent = Literal[
    "interested",
    "question",
    "provided_info",
    "reschedule_request",
    "not_interested",
    "unclear",
]


def _clamp(value: object, low: float, high: float) -> float:
    """Привести недоверенное число к диапазону [low, high].

    Модели возвращают ``120``, ``-5``, ``"0.8"``, ``null`` и ``NaN``. Все эти
    случаи нужно пережить: значение нормализуется, а не роняет обработку.
    """
    if isinstance(value, bool | int | float):
        number = float(value)
    elif isinstance(value, str):
        try:
            number = float(value.strip())
        except ValueError:
            return low
    else:
        return low
    if number != number:  # NaN
        return low
    return max(low, min(high, number))


class CriterionScoreSchema(BaseModel):
    """Оценка одного критерия от модели."""

    model_config = ConfigDict(extra="ignore")

    key: str
    score: float = 0.0
    evidence: str = ""

    @field_validator("score", mode="before")
    @classmethod
    def _normalize_score(cls, value: object) -> float:
        return _clamp(value, 0.0, 100.0)

    @field_validator("evidence", mode="before")
    @classmethod
    def _normalize_evidence(cls, value: object) -> str:
        return value.strip() if isinstance(value, str) else ""


class ScreeningResponseSchema(BaseModel):
    """Ответ модели на скрининг резюме.

    ``total_score`` намеренно отсутствует: балл считаем сами. Если модель всё же
    вернула своё число, ``extra="ignore"`` его отбросит.
    """

    model_config = ConfigDict(extra="ignore")

    criteria: list[CriterionScoreSchema] = Field(default_factory=list)
    summary: str = ""
    gaps: list[str] = Field(default_factory=list)
    strengths: list[str] = Field(default_factory=list)
    questions: list[str] = Field(default_factory=list)

    @field_validator("gaps", "strengths", "questions", mode="before")
    @classmethod
    def _normalize_list(cls, value: object) -> list[str]:
        if isinstance(value, str):
            return [value.strip()] if value.strip() else []
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]
        return []

    @field_validator("summary", mode="before")
    @classmethod
    def _normalize_summary(cls, value: object) -> str:
        return value.strip() if isinstance(value, str) else ""


class MessageResponseSchema(BaseModel):
    """Ответ модели на генерацию сообщения."""

    model_config = ConfigDict(extra="ignore")

    text: str

    @field_validator("text", mode="before")
    @classmethod
    def _require_text(cls, value: object) -> str:
        text = value.strip() if isinstance(value, str) else ""
        if not text:
            msg = "Модель вернула пустой текст сообщения"
            raise ValueError(msg)
        return text


class ReplyAnalysisResponseSchema(BaseModel):
    """Разбор ответа кандидата."""

    model_config = ConfigDict(extra="ignore")

    intent: Intent
    summary: str = ""
    extracted: dict[str, Any] = Field(default_factory=dict)
    chosen_slot_index: int | None = None
    confidence: float = 0.0

    @field_validator("confidence", mode="before")
    @classmethod
    def _normalize_confidence(cls, value: object) -> float:
        return _clamp(value, 0.0, 1.0)

    @field_validator("chosen_slot_index", mode="before")
    @classmethod
    def _normalize_slot_index(cls, value: object) -> int | None:
        """Номер слота: только положительное целое, иначе «выбор не выражен»."""
        if value is None or isinstance(value, bool):
            return None
        if isinstance(value, int):
            return value if value > 0 else None
        if isinstance(value, float):
            index = int(value)
            return index if index > 0 else None
        if isinstance(value, str):
            text = value.strip()
            if not text.isdigit():
                return None
            index = int(text)
            return index if index > 0 else None
        return None

    @field_validator("extracted", mode="before")
    @classmethod
    def _normalize_extracted(cls, value: object) -> dict[str, Any]:
        if not isinstance(value, dict):
            return {}
        return {str(key): item for key, item in value.items() if item not in (None, "")}

    @field_validator("summary", mode="before")
    @classmethod
    def _normalize_summary(cls, value: object) -> str:
        return value.strip() if isinstance(value, str) else ""
