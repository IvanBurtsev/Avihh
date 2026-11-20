"""Калибровка вердиктов: strong_match не должен выдаваться уверенному middle.

Порог приглашения остаётся прежним (70), но «сильное соответствие» требует
запаса балла и закрытых с оценкой >= 85 обязательных критериев. Тесты фиксируют
границы, чтобы понижение порога не вернулось незаметно.
"""

from __future__ import annotations

import pytest

from hirebot.domain.entities import ScreeningCriterion
from hirebot.domain.enums import ScreeningVerdict
from hirebot.ports.llm import ScreeningDraft
from hirebot.services.screening import (
    MUST_HAVE_SCORE_CAP,
    STRONG_MATCH_MARGIN,
    evaluate_draft,
    meaningful_characters,
    weighted_score,
)

MIN_SCORE = 70.0


def criterion(
    key: str,
    score: float,
    *,
    weight: float = 1.0,
    must_have: bool = False,
    is_gap: bool = False,
) -> ScreeningCriterion:
    return ScreeningCriterion(
        key=key,
        title=key,
        weight=weight,
        score=score,
        must_have=must_have,
        evidence="цитата",
        is_gap=is_gap,
    )


def test_strong_match_requires_margin_and_closed_must_haves() -> None:
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[
            criterion("python", 98.0, weight=3.0, must_have=True),
            criterion("sql", 94.0, weight=2.0, must_have=True),
            criterion("api", 92.0, weight=2.0),
            criterion("docker", 90.0, weight=1.0),
            criterion("english", 85.0, weight=1.0),
        ],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=MIN_SCORE)

    assert outcome.score >= MIN_SCORE + STRONG_MATCH_MARGIN
    assert outcome.verdict is ScreeningVerdict.STRONG_MATCH


def test_same_profile_without_margin_is_match_not_strong() -> None:
    """Балл 85..92 — это match: сильного запаса нет."""
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[
            criterion("python", 94.0, weight=3.0, must_have=True),
            criterion("sql", 90.0, weight=2.0, must_have=True),
            criterion("api", 88.0, weight=2.0),
            criterion("docker", 86.0, weight=1.0),
            criterion("english", 80.0, weight=1.0),
        ],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=MIN_SCORE)

    assert 85.0 <= outcome.score < MIN_SCORE + STRONG_MATCH_MARGIN
    assert outcome.verdict is ScreeningVerdict.MATCH


def test_must_have_below_85_blocks_strong_match() -> None:
    """Высокий средний балл не спасает обязательный критерий с оценкой 84."""
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[
            criterion("python", 100.0, weight=3.0, must_have=True),
            criterion("sql", 84.0, weight=1.0, must_have=True),
            criterion("api", 100.0, weight=3.0),
            criterion("docker", 100.0, weight=2.0),
            criterion("english", 100.0, weight=1.0),
        ],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=MIN_SCORE)

    assert outcome.score >= MIN_SCORE + STRONG_MATCH_MARGIN
    assert outcome.verdict is ScreeningVerdict.MATCH


@pytest.mark.parametrize("gap_score", [30.0, 59.0])
def test_noticeable_gap_blocks_strong_match(gap_score: float) -> None:
    """Заметный пробел в необязательном критерии — тоже не strong_match."""
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[
            criterion("python", 100.0, weight=6.0, must_have=True),
            criterion("sql", 100.0, weight=1.0, must_have=True),
            criterion("api", 100.0, weight=1.0),
            criterion("docker", gap_score, weight=1.0, is_gap=True),
            criterion("english", 100.0, weight=1.0),
        ],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=MIN_SCORE)

    assert outcome.verdict is not ScreeningVerdict.STRONG_MATCH


def test_must_have_failure_still_caps_score_and_verdict() -> None:
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[
            criterion("python", 100.0, weight=3.0, must_have=True),
            criterion("sql", 80.0, weight=2.0, must_have=True, is_gap=True),
            criterion("api", 100.0, weight=2.0),
            criterion("docker", 100.0, weight=1.0),
            criterion("english", 100.0, weight=1.0),
        ],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=MIN_SCORE)

    assert weighted_score(outcome.criteria) > MIN_SCORE + STRONG_MATCH_MARGIN
    assert outcome.score == MUST_HAVE_SCORE_CAP
    assert outcome.verdict is ScreeningVerdict.WEAK


def test_meaningful_characters_ignores_spaces_and_punctuation() -> None:
    assert meaningful_characters("   ...  ") == 0
    assert meaningful_characters("Python, SQL.") == 9
    assert meaningful_characters("Резюме.\nКонтакты: скрыты.") < 60
