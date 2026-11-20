"""Метрики качества скрининга.

Замер ведётся в двух плоскостях, и это принципиально:

* **бинарное решение «приглашать / не приглашать»** — то, что определяет
  бизнес-эффект. Ложное приглашение тратит время рекрутера и кандидата,
  ложный отказ теряет человека; поэтому precision/recall считаются именно
  по этому решению, а не по «красивому» вердикту;
* **точность вердикта и точность по провалам must-have** — диагностика.
  Они показывают, где именно расходится логика: в оценке обязательного
  критерия, в пороге или в самом построении вердикта.

Нулевые знаменатели не являются ошибкой: если модель никого не пригласила,
precision не определена, и возвращать ``0.0`` честнее, чем делить на ноль.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from hirebot.domain.enums import ScreeningVerdict

__all__ = [
    "BinaryMetrics",
    "CaseOutcome",
    "EvaluationReport",
    "binary_metrics",
]


@dataclass(frozen=True, slots=True)
class BinaryMetrics:
    """Матрица ошибок бинарного решения и производные метрики."""

    true_positive: int
    false_positive: int
    true_negative: int
    false_negative: int

    @property
    def precision(self) -> float:
        """Доля правильных приглашений среди всех приглашений."""
        denominator = self.true_positive + self.false_positive
        return self.true_positive / denominator if denominator else 0.0

    @property
    def recall(self) -> float:
        """Доля приглашённых среди тех, кого человек пригласил бы."""
        denominator = self.true_positive + self.false_negative
        return self.true_positive / denominator if denominator else 0.0

    @property
    def f1(self) -> float:
        """Гармоническое среднее precision и recall."""
        precision = self.precision
        recall = self.recall
        denominator = precision + recall
        return 2 * precision * recall / denominator if denominator else 0.0

    @property
    def accuracy(self) -> float:
        """Доля случаев, где бинарное решение совпало с разметкой."""
        total = self.true_positive + self.false_positive + self.true_negative + self.false_negative
        return (self.true_positive + self.true_negative) / total if total else 0.0

    @property
    def specificity(self) -> float:
        """Доля правильных отказов среди тех, кого человек не пригласил бы."""
        denominator = self.true_negative + self.false_positive
        return self.true_negative / denominator if denominator else 0.0


def binary_metrics(*, predicted: Sequence[bool], expected: Sequence[bool]) -> BinaryMetrics:
    """Посчитать матрицу ошибок по двум параллельным последовательностям.

    ``predicted`` — решение harness, ``expected`` — разметка человека.
    Последовательности обязаны быть одной длины: молчаливое усечение
    ``zip`` — классический способ получить красивые и неверные метрики.
    """
    if len(predicted) != len(expected):
        msg = f"predicted и expected разной длины: {len(predicted)} != {len(expected)}"
        raise ValueError(msg)

    true_positive = 0
    false_positive = 0
    true_negative = 0
    false_negative = 0
    for actual, reference in zip(predicted, expected, strict=True):
        if actual and reference:
            true_positive += 1
        elif actual and not reference:
            false_positive += 1
        elif not actual and not reference:
            true_negative += 1
        else:
            false_negative += 1
    return BinaryMetrics(
        true_positive=true_positive,
        false_positive=false_positive,
        true_negative=true_negative,
        false_negative=false_negative,
    )


@dataclass(frozen=True, slots=True)
class CaseOutcome:
    """Результат одного случая: что ожидал человек и что решил harness."""

    case_id: str
    title: str
    expected_verdict: ScreeningVerdict
    actual_verdict: ScreeningVerdict
    expected_should_invite: bool
    actual_should_invite: bool
    score: float
    must_have_gaps: tuple[str, ...]
    expected_must_have_gaps: tuple[str, ...]
    notes: str = ""

    @property
    def verdict_matches(self) -> bool:
        """Совпал ли вердикт целиком (диагностическая метрика)."""
        return self.actual_verdict == self.expected_verdict

    @property
    def decision_matches(self) -> bool:
        """Совпало ли бинарное решение «приглашать / не приглашать»."""
        return self.actual_should_invite == self.expected_should_invite

    @property
    def must_have_matches(self) -> bool:
        """Совпал ли набор проваленных обязательных критериев."""
        return set(self.must_have_gaps) == set(self.expected_must_have_gaps)


@dataclass(frozen=True, slots=True)
class EvaluationReport:
    """Итог прогона: построчные исходы и агрегированные метрики."""

    dataset_version: str
    model: str
    outcomes: tuple[CaseOutcome, ...]
    decision_metrics: BinaryMetrics

    @property
    def verdict_accuracy(self) -> float:
        """Доля случаев с точным совпадением вердикта."""
        if not self.outcomes:
            return 0.0
        return sum(1 for outcome in self.outcomes if outcome.verdict_matches) / len(self.outcomes)

    @property
    def must_have_accuracy(self) -> float:
        """Доля случаев с точным совпадением набора провалов must-have."""
        if not self.outcomes:
            return 0.0
        return sum(1 for outcome in self.outcomes if outcome.must_have_matches) / len(self.outcomes)

    @property
    def failures(self) -> tuple[CaseOutcome, ...]:
        """Случаи, где бинарное решение разошлось с разметкой человека."""
        return tuple(outcome for outcome in self.outcomes if not outcome.decision_matches)
