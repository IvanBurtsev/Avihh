"""Тесты домена скоринга: процент, уверенность, блокеры, приоритет, интервал.

Проверяется именно математика и правила, потому что от них зависит решение по
кандидату: ошибка здесь не «косметическая», а кадровая.
"""

from __future__ import annotations

import pytest

from hirebot.domain.enums import DecisionKind, ProfileStatus
from hirebot.domain.scoring import (
    CriterionScore,
    ProfileCriterion,
    ProfileQuestion,
    ProfileThresholds,
    ScoringProfile,
    apply_must_have_cap,
    availability_score,
    compute_breakdown,
    freshness_score,
    normalized_weights,
    question_value,
)


def _criteria() -> list[ProfileCriterion]:
    return [
        ProfileCriterion(key="python", title="Python", weight=5, must_have=True),
        ProfileCriterion(key="sql", title="SQL", weight=3),
        ProfileCriterion(key="english", title="Английский", weight=1),
    ]


def _scores(**overrides: float) -> dict[str, CriterionScore]:
    base = {
        "python": CriterionScore(
            key="python",
            title="Python",
            weight=5,
            score=90.0,
            must_have=True,
            evidence="5 лет Python",
        ),
        "sql": CriterionScore(key="sql", title="SQL", weight=3, score=70.0, evidence="PostgreSQL"),
        "english": CriterionScore(key="english", title="Английский", weight=1, score=50.0, evidence="B1"),
    }
    for key, value in overrides.items():
        item = base[key]
        base[key] = CriterionScore(
            key=item.key,
            title=item.title,
            weight=item.weight,
            score=value,
            must_have=item.must_have,
            evidence=item.evidence,
        )
    return base


# ---------------------------------------------------------------- веса


def test_weights_are_normalized_to_shares() -> None:
    weights = normalized_weights(_criteria())

    assert sum(weights.values()) == pytest.approx(1.0)
    assert weights["python"] == pytest.approx(5 / 9)
    assert weights["english"] == pytest.approx(1 / 9)


def test_zero_weight_is_rejected_by_contract() -> None:
    """Вес от 1 до 5: нулевой критерий означал бы «выбран, но ни на что не влияет»."""
    with pytest.raises(ValueError, match="от 1 до 5"):
        ProfileCriterion(key="a", title="A", weight=0)


def test_empty_criteria_give_empty_weights() -> None:
    assert normalized_weights([]) == {}


# ---------------------------------------------------------------- процент


def test_percent_is_weighted_by_profile() -> None:
    breakdown = compute_breakdown(_criteria(), _scores())

    # (90*5 + 70*3 + 50*1) / 9 = 710/9 = 78.9
    assert breakdown.percent == pytest.approx(78.9, abs=0.1)
    assert breakdown.coverage == pytest.approx(1.0)
    assert breakdown.blockers == ()


def test_missing_criterion_lowers_coverage_not_percent_alone() -> None:
    """Нет данных — это не провал: процент без подтверждения не растёт."""
    scores = _scores()
    scores["sql"] = CriterionScore(key="sql", title="SQL", weight=3, score=0.0, evidence="", is_gap=False)

    breakdown = compute_breakdown(_criteria(), scores)

    assert breakdown.coverage < 1.0, "неподтверждённый критерий обязан снижать уверенность"
    assert breakdown.blockers == (), "отсутствие данных не блокер"
    assert not breakdown.is_confident


def test_blocker_detected_for_failed_must_have() -> None:
    scores = _scores()
    scores["python"] = CriterionScore(
        key="python",
        title="Python",
        weight=5,
        score=30.0,
        must_have=True,
        evidence="учусь",
        is_gap=True,
    )

    breakdown = compute_breakdown(_criteria(), scores)

    assert breakdown.has_blocker
    assert breakdown.blockers == ("Python",)


def test_must_have_failure_caps_percent() -> None:
    """Без обязательного навыка кандидат не обгоняет того, у кого он есть."""
    scores = _scores(sql=100.0, english=100.0)
    scores["python"] = CriterionScore(
        key="python",
        title="Python",
        weight=5,
        score=20.0,
        must_have=True,
        evidence="",
        is_gap=True,
    )
    breakdown = compute_breakdown(_criteria(), scores)

    capped = apply_must_have_cap(breakdown)

    assert capped.percent <= 40.0
    assert capped.coverage == breakdown.coverage


def test_cap_does_not_raise_low_percent() -> None:
    """Потолок не должен поднимать процент: он только ограничивает сверху."""
    scores = {
        "python": CriterionScore(
            key="python",
            title="Python",
            weight=5,
            score=10.0,
            must_have=True,
            evidence="",
            is_gap=True,
        )
    }
    breakdown = compute_breakdown(_criteria()[:1], scores)

    capped = apply_must_have_cap(breakdown)

    assert capped.percent == pytest.approx(breakdown.percent)


# ---------------------------------------------------------------- пороги


def test_thresholds_reject_invalid_order() -> None:
    with pytest.raises(ValueError, match="по возрастанию"):
        ProfileThresholds(invite_at=50.0, human_at=70.0, reject_at=30.0)


def test_thresholds_require_all_three_conditions_for_invite() -> None:
    thresholds = ProfileThresholds(invite_at=85.0, min_coverage_for_auto=0.7)

    assert thresholds.decide(percent=90.0, coverage=0.9, has_blocker=False) is DecisionKind.INVITE
    # Блокер: приглашения нет даже при высоком проценте.
    assert thresholds.decide(percent=95.0, coverage=0.9, has_blocker=True) is DecisionKind.HUMAN
    # Мало данных: тоже человеку, а не приглашение.
    assert thresholds.decide(percent=95.0, coverage=0.4, has_blocker=False) is DecisionKind.HUMAN


def test_thresholds_reject_below_floor() -> None:
    thresholds = ProfileThresholds(invite_at=85.0, human_at=55.0, reject_at=40.0)

    assert thresholds.decide(percent=20.0, coverage=1.0, has_blocker=False) is DecisionKind.REJECT
    assert thresholds.decide(percent=60.0, coverage=1.0, has_blocker=False) is DecisionKind.HUMAN


def test_question_limit_cannot_exceed_five() -> None:
    with pytest.raises(ValueError, match="max_questions"):
        ProfileThresholds(max_questions=6)


# ---------------------------------------------------------------- профиль


def _profile(**overrides: object) -> ScoringProfile:
    from uuid import uuid4

    defaults: dict[str, object] = {
        "id": uuid4(),
        "company_id": uuid4(),
        "vacancy_id": uuid4(),
        "version": 1,
        "status": ProfileStatus.DRAFT,
        "criteria": _criteria(),
        "questions": [
            ProfileQuestion(text="Сколько лет на Python?", criterion_key="python"),
            ProfileQuestion(text="Работали с PostgreSQL?", criterion_key="sql"),
        ],
    }
    defaults.update(overrides)
    return ScoringProfile(**defaults)  # type: ignore[arg-type]


def test_profile_requires_must_have_criterion() -> None:
    profile = _profile(criteria=[ProfileCriterion(key="sql", title="SQL", weight=3, must_have=False)])

    with pytest.raises(ValueError, match="обязательный"):
        profile.validate()


def test_profile_rejects_question_for_missing_criterion() -> None:
    profile = _profile(questions=[ProfileQuestion(text="О чём?", criterion_key="нет-такого")])

    with pytest.raises(ValueError, match="которого нет в профиле"):
        profile.validate()


def test_profile_rejects_custom_criterion_without_evidence_hint() -> None:
    profile = _profile(
        criteria=[
            ProfileCriterion(key="python", title="Python", weight=5, must_have=True),
            ProfileCriterion(key="custom", title="Своё", weight=2, source="custom"),
        ],
        questions=[],
    )

    with pytest.raises(ValueError, match="что искать"):
        profile.validate()


def test_draft_cannot_be_published_without_version() -> None:
    profile = _profile(version=0)

    with pytest.raises(ValueError, match="начиная с 1"):
        profile.validate()


def test_as_vacancy_dict_keeps_expectation() -> None:
    """Ожидание («не менее 3 лет») должно доходить до модели, иначе оценка размыта."""
    criterion = ProfileCriterion(
        key="python",
        title="Python",
        weight=5,
        must_have=True,
        expectation="не менее 3 лет коммерческой разработки",
    )

    payload = criterion.as_vacancy_dict()

    assert payload["expectation"] == "не менее 3 лет коммерческой разработки"


# ---------------------------------------------------------------- приоритет


def test_freshness_decays_with_half_life() -> None:
    assert freshness_score(0) == pytest.approx(100.0)
    assert freshness_score(24) == pytest.approx(50.0)
    assert freshness_score(48) == pytest.approx(25.0)


def test_availability_unknown_is_neutral() -> None:
    """Неизвестная готовность не должна наказывать кандидата."""
    assert availability_score(ready_days=None) == pytest.approx(50.0)
    assert availability_score(ready_days=0) == pytest.approx(100.0)
    assert availability_score(ready_days=30) == pytest.approx(0.0)


def test_question_value_prefers_must_have_without_data() -> None:
    must_have = ProfileCriterion(key="python", title="Python", weight=5, must_have=True)
    secondary = ProfileCriterion(key="english", title="Английский", weight=1)
    question = ProfileQuestion(text="?", criterion_key="python", informativeness=3)
    weights = normalized_weights([must_have, secondary])

    high = question_value(question, must_have, coverage=0.0, weights=weights)
    low = question_value(question, secondary, coverage=0.0, weights=weights)

    assert high > low


def test_answered_criterion_has_low_question_value() -> None:
    criterion = ProfileCriterion(key="python", title="Python", weight=5, must_have=True)
    question = ProfileQuestion(text="?", criterion_key="python", informativeness=3)
    weights = normalized_weights([criterion])

    covered = question_value(question, criterion, coverage=1.0, weights=weights)

    assert covered == pytest.approx(0.0)
