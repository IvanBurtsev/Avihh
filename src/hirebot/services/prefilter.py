"""Предскоринг отклика до открытия резюме.

Зачем: открытие резюме на hh.ru и Avito может списывать деньги. Если отклик
заведомо не по профилю, платить за его резюме незачем — лучше сразу отдать
человеку.

Что используется: **сопроводительное письмо** и сырые метаданные отклика
(заголовок, город, зарплата). Это то, что приходит вместе с откликом и не
требует платного вызова.

Чего здесь намеренно нет:

* **автоматического отказа** — только «не тратить деньги и позвать человека»;
* **решения по пустым данным** — если письма нет, оценка не делается, и
  отклик проходит обычным путём. Наказывать за отсутствие данных нельзя.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any

from hirebot.domain.entities import Vacancy
from hirebot.domain.scoring import ScoringProfile
from hirebot.ports.dto import InboundResponse

#: Слова, которые ничего не говорят о соответствии. Иначе «опыт работы» в
#: каждом письме давал бы ложное совпадение.
STOPWORDS: frozenset[str] = frozenset(
    {
        "и",
        "в",
        "на",
        "с",
        "по",
        "для",
        "не",
        "что",
        "как",
        "это",
        "или",
        "опыт",
        "работы",
        "работа",
        "работал",
        "работала",
        "компании",
        "проект",
        "проекты",
        "задачи",
        "умею",
        "знаю",
        "есть",
        "был",
        "была",
        "the",
        "and",
        "with",
        "for",
        "from",
        "work",
        "experience",
    }
)

#: Минимальная длина значимого слова.
MIN_TOKEN_LEN = 4

#: Сколько символов текста анализировать: длинное письмо не даёт больше сигнала,
#: но замедляет обработку.
MAX_ANALYZED_CHARS = 4000


@dataclass(slots=True, frozen=True)
class PreliminaryScore:
    """Результат предскоринга.

    ``has_data`` отделяет «плохой кандидат» от «нет данных»: во втором случае
    решение принимать нельзя, отклик идёт обычным путём.
    """

    score: float
    has_data: bool
    matched: tuple[str, ...] = ()
    reason: str = ""

    @property
    def is_weak(self) -> bool:
        return self.has_data and self.score < 0


@dataclass(slots=True)
class PrefilterSettings:
    """Порог и включение предскоринга."""

    enabled: bool = True
    #: Ниже этого балла резюме не открывается (0..100).
    min_score: float = 20.0
    #: Вес совпадения по сопроводительному письму.
    cover_letter_weight: float = 0.7
    #: Вес совпадения по сырым метаданным (заголовок, город).
    metadata_weight: float = 0.3


def _tokens(text: str) -> set[str]:
    """Значимые слова текста в нижнем регистре."""
    words = re.findall(r"[a-zA-Zа-яА-ЯёЁ0-9+#.]+", (text or "").lower())
    return {word for word in words if len(word) >= MIN_TOKEN_LEN and word not in STOPWORDS}


def _criterion_tokens(criteria: list[Any]) -> set[str]:
    """Слова, по которым можно судить о соответствии критериям."""
    tokens: set[str] = set()
    for item in criteria:
        tokens |= _tokens(str(getattr(item, "title", "") or ""))
        tokens |= _tokens(str(getattr(item, "expectation", "") or ""))
        tokens |= _tokens(str(getattr(item, "evidence_hint", "") or ""))
        key = str(getattr(item, "key", "") or "")
        if key:
            tokens.add(key.lower())
    return tokens


def _metadata_text(response: InboundResponse) -> str:
    """Текст из сырых метаданных отклика: заголовок, город, зарплата."""
    parts: list[str] = []
    raw = response.raw if isinstance(response.raw, dict) else {}
    for key in ("title", "resume_title", "position", "area", "city", "salary", "name"):
        value = raw.get(key)
        if isinstance(value, str) and value.strip():
            parts.append(value)
        elif isinstance(value, dict):
            nested = value.get("name") or value.get("title")
            if isinstance(nested, str) and nested.strip():
                parts.append(nested)
    return " ".join(parts)


def score_response(
    *,
    vacancy: Vacancy,
    response: InboundResponse,
    profile: ScoringProfile | None,
    settings: PrefilterSettings | None = None,
) -> PreliminaryScore:
    """Оценить, стоит ли открывать резюме.

    Балл — доля слов критериев, найденных в письме и метаданных, в шкале 0..100.
    Это грубая оценка, и она честно называется грубой: её задача — отсечь явно
    нерелевантное, а не заменить полноценный скоринг.
    """
    resolved = settings or PrefilterSettings()
    criteria = profile.criteria if profile is not None else []
    # Без профиля судим по заголовку вакансии: этого мало, но лучше, чем ничего,
    # и точно не блокирует отклик.

    expected = _criterion_tokens(list(criteria))
    if not expected:
        expected = _tokens(vacancy.title)
    if not expected:
        return PreliminaryScore(score=0.0, has_data=False, reason="не из чего собрать ожидания по вакансии")

    letter = (response.cover_letter or "")[:MAX_ANALYZED_CHARS]
    metadata = _metadata_text(response)
    if not letter.strip() and not metadata.strip():
        return PreliminaryScore(
            score=0.0,
            has_data=False,
            reason="нет сопроводительного письма и метаданных — решать не по чему",
        )

    letter_hits = expected & _tokens(letter)
    metadata_hits = expected & _tokens(metadata)

    letter_share = len(letter_hits) / len(expected) if expected else 0.0
    metadata_share = len(metadata_hits) / len(expected) if expected else 0.0
    combined = resolved.cover_letter_weight * letter_share + resolved.metadata_weight * metadata_share
    score = round(min(100.0, combined * 100.0 * 2), 1)

    matched = tuple(sorted(letter_hits | metadata_hits))
    return PreliminaryScore(
        score=score,
        has_data=True,
        matched=matched,
        reason=(
            f"совпало {len(matched)} из {len(expected)} значимых слов критериев"
            if matched
            else "совпадений с критериями не найдено"
        ),
    )


def should_skip_resume(
    *,
    preliminary: PreliminaryScore,
    settings: PrefilterSettings | None = None,
) -> bool:
    """Стоит ли пропустить платное открытие резюме.

    Пропускаем только при наличии данных и низком балле. Отсутствие данных —
    не повод пропускать: иначе кандидаты без письма отсекались бы все подряд.
    """
    resolved = settings or PrefilterSettings()
    if not resolved.enabled:
        return False
    if not preliminary.has_data:
        return False
    return preliminary.score < resolved.min_score


__all__ = [
    "MAX_ANALYZED_CHARS",
    "MIN_TOKEN_LEN",
    "STOPWORDS",
    "PrefilterSettings",
    "PreliminaryScore",
    "score_response",
    "should_skip_resume",
]
