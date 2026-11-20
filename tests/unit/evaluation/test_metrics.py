"""Тесты метрик на синтетических данных с аналитически известным ответом.

Числа здесь считаются вручную, а не берутся из прогона: иначе тест проверяет
«код согласен сам с собой», а не правильность формул.
"""

from __future__ import annotations

import pytest

from hirebot.domain.enums import ScreeningVerdict
from hirebot.evaluation.metrics import (
    CaseOutcome,
    EvaluationReport,
    binary_metrics,
)

# predicted:  [T, T, T, F, F, F]
# expected:   [T, T, F, T, T, F]
# TP=2, FP=1, TN=1, FN=2
_PREDICTED = [True, True, True, False, False, False]
_EXPECTED = [True, True, False, True, True, False]


def test_confusion_matrix_counts() -> None:
    metrics = binary_metrics(predicted=_PREDICTED, expected=_EXPECTED)
    assert metrics.true_positive == 2
    assert metrics.false_positive == 1
    assert metrics.true_negative == 1
    assert metrics.false_negative == 2


def test_precision_recall_f1_accuracy_and_specificity() -> None:
    metrics = binary_metrics(predicted=_PREDICTED, expected=_EXPECTED)
    assert metrics.precision == pytest.approx(2 / 3)
    assert metrics.recall == pytest.approx(1 / 2)
    assert metrics.f1 == pytest.approx(4 / 7)
    assert metrics.accuracy == pytest.approx(1 / 2)
    assert metrics.specificity == pytest.approx(1 / 2)


def test_empty_input_gives_zero_metrics_without_division_error() -> None:
    metrics = binary_metrics(predicted=[], expected=[])
    assert (metrics.true_positive, metrics.false_positive, metrics.true_negative, metrics.false_negative) == (
        0,
        0,
        0,
        0,
    )
    assert metrics.precision == 0.0
    assert metrics.recall == 0.0
    assert metrics.f1 == 0.0
    assert metrics.accuracy == 0.0
    assert metrics.specificity == 0.0


def test_no_positive_predictions_has_zero_precision() -> None:
    metrics = binary_metrics(predicted=[False, False], expected=[True, False])
    assert metrics.precision == 0.0
    assert metrics.f1 == 0.0
    assert metrics.accuracy == 0.5


def test_no_positive_expected_has_zero_recall() -> None:
    metrics = binary_metrics(predicted=[True, False], expected=[False, False])
    assert metrics.recall == 0.0
    # Приглашение было одно, и оно ложное: precision = 0.
    assert metrics.precision == 0.0
    assert metrics.specificity == 0.5


def test_all_predictions_positive_and_expected_positive() -> None:
    metrics = binary_metrics(predicted=[True, True], expected=[True, True])
    assert metrics.precision == 1.0
    assert metrics.recall == 1.0
    assert metrics.f1 == 1.0
    assert metrics.accuracy == 1.0
    # Отрицательных примеров не было: specificity не определена -> 0.0.
    assert metrics.specificity == 0.0


def test_length_mismatch_is_an_error() -> None:
    with pytest.raises(ValueError, match="разной длины"):
        binary_metrics(predicted=[True], expected=[True, False])


def _outcome(
    case_id: str,
    expected_verdict: ScreeningVerdict,
    actual_verdict: ScreeningVerdict,
    *,
    expected_invite: bool,
    actual_invite: bool,
    actual_gaps: tuple[str, ...],
    expected_gaps: tuple[str, ...],
) -> CaseOutcome:
    return CaseOutcome(
        case_id=case_id,
        title=case_id,
        expected_verdict=expected_verdict,
        actual_verdict=actual_verdict,
        expected_should_invite=expected_invite,
        actual_should_invite=actual_invite,
        score=70.0,
        must_have_gaps=actual_gaps,
        expected_must_have_gaps=expected_gaps,
    )


def test_case_outcome_match_flags() -> None:
    outcome = _outcome(
        "c1",
        ScreeningVerdict.MATCH,
        ScreeningVerdict.MATCH,
        expected_invite=True,
        actual_invite=True,
        actual_gaps=("sql",),
        expected_gaps=("sql",),
    )
    assert outcome.verdict_matches is True
    assert outcome.decision_matches is True
    assert outcome.must_have_matches is True


def test_report_aggregates_outcomes() -> None:
    outcomes = (
        _outcome(
            "c1",
            ScreeningVerdict.MATCH,
            ScreeningVerdict.MATCH,
            expected_invite=True,
            actual_invite=True,
            actual_gaps=("sql",),
            expected_gaps=("sql",),
        ),
        _outcome(
            "c2",
            ScreeningVerdict.MATCH,
            ScreeningVerdict.WEAK,
            expected_invite=True,
            actual_invite=False,
            actual_gaps=("python", "sql"),
            expected_gaps=("sql",),
        ),
        _outcome(
            "c3",
            ScreeningVerdict.WEAK,
            ScreeningVerdict.WEAK,
            expected_invite=False,
            actual_invite=False,
            actual_gaps=("python",),
            expected_gaps=("python",),
        ),
    )
    report = EvaluationReport(
        dataset_version="test",
        model="synthetic",
        outcomes=outcomes,
        decision_metrics=binary_metrics(
            predicted=[outcome.actual_should_invite for outcome in outcomes],
            expected=[outcome.expected_should_invite for outcome in outcomes],
        ),
    )

    assert report.decision_metrics.true_positive == 1
    assert report.decision_metrics.false_negative == 1
    assert report.decision_metrics.true_negative == 1
    assert report.verdict_accuracy == pytest.approx(2 / 3)
    assert report.must_have_accuracy == pytest.approx(2 / 3)
    assert report.failures == (outcomes[1],)


def test_empty_report_has_zero_accuracies() -> None:
    report = EvaluationReport(
        dataset_version="test",
        model="synthetic",
        outcomes=(),
        decision_metrics=binary_metrics(predicted=[], expected=[]),
    )
    assert report.verdict_accuracy == 0.0
    assert report.must_have_accuracy == 0.0
    assert report.failures == ()
