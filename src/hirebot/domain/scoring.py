"""Домен скоринга: профиль вакансии, процент соответствия, приоритет.

Почему это отдельный модуль, а не часть ``entities``.

Профиль скоринга — это **настройка под вакансию**, которая меняется: критерии,
вопросы, пороги. Вместе с ней живёт математика соответствия и приоритета, и
именно её чаще всего переписывают «по месту». Здесь она собрана так, чтобы:

* процент считался **одинаково** в скрининге, в пересчёте после ответов и в UI;
* было видно, почему результат такой: вклад каждого критерия, уверенность,
  блокеры;
* веса нормализовались в доли — процент не «прыгает» от числа критериев.

Ключевые определения:

* ``percent`` — взвешенное закрытие критериев, 0…100;
* ``coverage`` — какая доля веса подтверждена данными (0…1). Процент без
  уверенности обманчив: 78% при данных по 4 из 6 критериев и 78% по всем шести —
  разные кандидаты;
* ``blockers`` — провалы обязательных критериев. Они не «вычитают балл», а
  исключают автоматическое приглашение.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from hirebot.domain.entities import _utcnow
from hirebot.domain.enums import (
    DecisionKind,
    ProfileStatus,
    QuestionKind,
    ThresholdMode,
)

#: Минимальный и максимальный вес критерия в интерфейсе.
MIN_WEIGHT = 1
MAX_WEIGHT = 5

#: Допустимый диапазон числа вопросов.
MIN_QUESTIONS = 1
MAX_QUESTIONS = 5


@dataclass(slots=True, frozen=True)
class ProfileCriterion:
    """Критерий внутри профиля скоринга.

    ``evidence_hint`` — что искать в резюме или в ответе. Без него модель не
    понимает, что считать доказательством, и критерий превращается в догадку.
    """

    key: str
    title: str
    weight: int = 3
    must_have: bool = False
    evidence_hint: str = ""
    #: Что именно требуется — «не менее 3 лет», «уверенное владение».
    #: Без ожидания оценка размывается: непонятно, какой уровень считать закрытым.
    expectation: str = ""
    source: str = "catalog"
    position: int = 0

    def __post_init__(self) -> None:
        if not MIN_WEIGHT <= self.weight <= MAX_WEIGHT:
            msg = f"Вес критерия {self.key!r} должен быть от {MIN_WEIGHT} до {MAX_WEIGHT}"
            raise ValueError(msg)
        if not self.key.strip():
            msg = "Ключ критерия не может быть пустым"
            raise ValueError(msg)

    def as_vacancy_dict(self) -> dict[str, Any]:
        """Формат, в котором критерий передаётся в промпт и хранится в вакансии."""
        return {
            "key": self.key,
            "title": self.title,
            "weight": float(self.weight),
            "must_have": self.must_have,
            "evidence_hint": self.evidence_hint,
            "expectation": self.expectation or self.title,
        }


@dataclass(slots=True, frozen=True)
class ProfileQuestion:
    """Вопрос профиля, привязанный к критерию.

    ``informativeness`` — насколько ответ меняет решение (1…3). Планировщик
    выбирает вопросы по ценности, а не по порядку в списке.
    """

    text: str
    criterion_key: str
    kind: QuestionKind = QuestionKind.FACT
    informativeness: int = 2
    is_custom: bool = False
    position: int = 0

    def __post_init__(self) -> None:
        if not 1 <= self.informativeness <= 3:
            msg = "informativeness должен быть от 1 до 3"
            raise ValueError(msg)
        if not self.text.strip():
            msg = "Текст вопроса не может быть пустым"
            raise ValueError(msg)


@dataclass(slots=True, frozen=True)
class ProfileThresholds:
    """Пороги и режим принятия решения.

    Три порога, а не один: между «пригласить» и «отказать» обязателен коридор,
    где решает человек. Иначе бот отказывает тем, кого рекрутер взял бы.
    """

    invite_at: float = 85.0
    human_at: float = 55.0
    reject_at: float = 40.0
    mode: ThresholdMode = ThresholdMode.ABSOLUTE
    #: Для относительного режима: сколько лучших считаем проходными.
    top_k: int = 10
    max_questions: int = MAX_QUESTIONS
    auto_invite: bool = False
    #: Минимальная уверенность для автоматического приглашения.
    min_coverage_for_auto: float = 0.7

    def __post_init__(self) -> None:
        if not (self.reject_at <= self.human_at <= self.invite_at):
            msg = (
                "Пороги должны идти по возрастанию: "
                f"reject_at ({self.reject_at}) ≤ human_at ({self.human_at}) ≤ invite_at ({self.invite_at})"
            )
            raise ValueError(msg)
        if not 0.0 <= self.reject_at <= 100.0 or not 0.0 <= self.invite_at <= 100.0:
            msg = "Пороги задаются в процентах от 0 до 100"
            raise ValueError(msg)
        if not MIN_QUESTIONS <= self.max_questions <= MAX_QUESTIONS:
            msg = f"max_questions должен быть от {MIN_QUESTIONS} до {MAX_QUESTIONS}"
            raise ValueError(msg)
        if not 0.0 <= self.min_coverage_for_auto <= 1.0:
            msg = "min_coverage_for_auto задаётся долей от 0 до 1"
            raise ValueError(msg)

    def decide(
        self,
        *,
        percent: float,
        coverage: float,
        has_blocker: bool,
    ) -> DecisionKind:
        """Решение по одному кандидату.

        Три условия автоматического приглашения: процент выше порога, нет
        блокеров, по обязательным критериям есть данные. Если хотя бы одно не
        выполнено — решение уходит человеку.
        """
        if percent >= self.invite_at and not has_blocker and coverage >= self.min_coverage_for_auto:
            return DecisionKind.INVITE
        if percent >= self.human_at:
            return DecisionKind.HUMAN
        if percent >= self.reject_at:
            return DecisionKind.HUMAN
        return DecisionKind.REJECT


@dataclass(slots=True)
class ScoringProfile:
    """Версионируемый профиль скоринга вакансии.

    Правки копятся в черновике и применяются публикацией. Уже обработанные
    кандидаты остаются посчитанными по своей версии — иначе нельзя объяснить
    прошлые решения.
    """

    id: UUID
    company_id: UUID
    vacancy_id: UUID
    version: int
    status: ProfileStatus
    criteria: list[ProfileCriterion] = field(default_factory=list)
    questions: list[ProfileQuestion] = field(default_factory=list)
    thresholds: ProfileThresholds = field(default_factory=ProfileThresholds)
    created_by: str = "system"
    created_at: datetime = field(default_factory=_utcnow)
    published_at: datetime | None = None
    comment: str = ""

    def __post_init__(self) -> None:
        # Версия 0 — «профиль не сохранён»: так выглядит набор критериев,
        # собранный из старой вакансии до появления версий. Публиковать его
        # нельзя, но обрабатывать кандидатов по нему нужно.
        if self.version < 0:
            msg = "Версия профиля не может быть отрицательной"
            raise ValueError(msg)

    @property
    def is_draft(self) -> bool:
        return self.status is ProfileStatus.DRAFT

    @property
    def is_live(self) -> bool:
        """Профиль применяется к новым кандидатам (в т.ч. в shadow-режиме)."""
        return self.status in {ProfileStatus.ACTIVE, ProfileStatus.SHADOW}

    @property
    def must_have_keys(self) -> list[str]:
        return [item.key for item in self.criteria if item.must_have]

    def criterion(self, key: str) -> ProfileCriterion | None:
        return next((item for item in self.criteria if item.key == key), None)

    def questions_for(self, criterion_key: str) -> list[ProfileQuestion]:
        return [item for item in self.questions if item.criterion_key == criterion_key]

    def validate(self) -> None:
        """Проверить профиль перед публикацией.

        Ошибки — исключение, а не «предупреждение»: несогласованный профиль
        нельзя применять к кандидатам, иначе решение будет случайным.
        """
        if self.version < 1:
            msg = "Опубликовать можно только сохранённую версию профиля (начиная с 1)"
            raise ValueError(msg)
        if not self.criteria:
            msg = "В профиле нет ни одного критерия: оценивать не по чему"
            raise ValueError(msg)
        if not any(item.must_have for item in self.criteria):
            msg = "Нужен хотя бы один обязательный критерий: иначе блокеров нет"
            raise ValueError(msg)
        if len(self.questions) > self.thresholds.max_questions:
            msg = f"Вопросов {len(self.questions)}, а лимит {self.thresholds.max_questions}"
            raise ValueError(msg)

        keys = {item.key for item in self.criteria}
        for question in self.questions:
            if question.criterion_key not in keys:
                msg = (
                    f"Вопрос «{question.text[:40]}» ссылается на критерий "
                    f"{question.criterion_key!r}, которого нет в профиле"
                )
                raise ValueError(msg)
        for criterion in self.criteria:
            if criterion.source == "custom" and not criterion.evidence_hint.strip():
                msg = (
                    f"У своего критерия {criterion.key!r} не указано, что искать: "
                    "без этого модель не сможет его оценить"
                )
                raise ValueError(msg)

    def to_questions_payload(self) -> dict[str, list[dict[str, Any]]]:
        """Сгруппировать вопросы по критериям для промпта."""
        grouped: dict[str, list[dict[str, Any]]] = {}
        for question in self.questions:
            grouped.setdefault(question.criterion_key, []).append(
                {"text": question.text, "kind": question.kind.value}
            )
        return grouped


@dataclass(slots=True, frozen=True)
class CriterionScore:
    """Оценка одного критерия: сколько, чем подтверждено, есть ли пробел."""

    key: str
    title: str
    weight: int
    score: float
    must_have: bool = False
    evidence: str = ""
    is_gap: bool = False
    #: Критерий оценён по ответу кандидата, а не по резюме.
    from_answer: bool = False

    def __post_init__(self) -> None:
        if not 0.0 <= self.score <= 100.0:
            msg = f"Оценка критерия {self.key!r} вне диапазона 0..100: {self.score}"
            raise ValueError(msg)


@dataclass(slots=True, frozen=True)
class ScoringBreakdown:
    """Результат скоринга с объяснением.

    ``percent`` — «насколько подходит», ``coverage`` — «насколько мы уверены»,
    ``blockers`` — «что мешает пригласить». Три числа, потому что одно врёт.
    """

    percent: float
    coverage: float
    blockers: tuple[str, ...]
    criteria: tuple[CriterionScore, ...]
    weights: dict[str, float]

    @property
    def has_blocker(self) -> bool:
        return bool(self.blockers)

    @property
    def is_confident(self) -> bool:
        return self.coverage >= 0.7

    def contribution(self, key: str) -> float:
        """Вклад критерия в итоговый процент — для объяснения решения."""
        criterion = next((item for item in self.criteria if item.key == key), None)
        if criterion is None:
            return 0.0
        return self.weights.get(key, 0.0) * criterion.score

    def as_dict(self) -> dict[str, Any]:
        return {
            "percent": round(self.percent, 1),
            "coverage": round(self.coverage, 3),
            "blockers": list(self.blockers),
            "criteria": [
                {
                    "key": item.key,
                    "score": round(item.score, 1),
                    "is_gap": item.is_gap,
                    "evidence": item.evidence,
                }
                for item in self.criteria
            ],
        }


def normalized_weights(criteria: list[ProfileCriterion]) -> dict[str, float]:
    """Привести веса к долям, сумма которых равна 1.

    Зачем: процент не должен зависеть от числа критериев. Пять критериев с весом
    3 и три с весом 3 — это разные наборы, но сумма весов одна, и без
    нормализации итоговые проценты несравнимы между вакансиями.
    """
    positive = [item for item in criteria if item.weight > 0]
    total = sum(item.weight for item in positive)
    if total <= 0:
        return {item.key: 0.0 for item in criteria}
    return {item.key: item.weight / total for item in positive}


def compute_breakdown(
    criteria: list[ProfileCriterion],
    scores: dict[str, CriterionScore],
) -> ScoringBreakdown:
    """Посчитать процент, уверенность и блокеры.

    Уверенность взвешенная: критерий с весом 5, по которому нет данных, снижает
    её сильнее, чем второстепенный.
    """
    weights = normalized_weights(criteria)
    if not weights:
        return ScoringBreakdown(
            percent=0.0, coverage=0.0, blockers=(), criteria=tuple(scores.values()), weights={}
        )

    percent = sum(weights.get(key, 0.0) * score.score for key, score in scores.items())
    covered = sum(weights.get(key, 0.0) for key, score in scores.items() if score.evidence.strip())
    blockers = tuple(item.title for item in scores.values() if item.must_have and item.is_gap)
    return ScoringBreakdown(
        percent=max(0.0, min(100.0, percent)),
        coverage=max(0.0, min(1.0, covered)),
        blockers=blockers,
        criteria=tuple(scores.values()),
        weights=weights,
    )


def apply_must_have_cap(breakdown: ScoringBreakdown, *, cap: float = 40.0) -> ScoringBreakdown:
    """Ограничить процент при провале обязательного критерия.

    Потолок, а не штраф: кандидат без обязательного навыка не должен обгонять
    того, у кого он есть, за счёт второстепенных критериев.
    """
    if not breakdown.has_blocker:
        return breakdown
    if breakdown.percent <= cap:
        return breakdown
    return ScoringBreakdown(
        percent=cap,
        coverage=breakdown.coverage,
        blockers=breakdown.blockers,
        criteria=breakdown.criteria,
        weights=breakdown.weights,
    )


# --------------------------------------------------------------------------- #
# Приоритет: кому писать первым
# --------------------------------------------------------------------------- #

#: Веса факторов приоритета. Настройки, а не константы в формуле: на защите
#: нужно уметь объяснить вклад каждого фактора и при желании изменить его.
DEFAULT_PRIORITY_WEIGHTS: dict[str, float] = {
    "percent": 0.70,
    "coverage": 0.15,
    "freshness": 0.10,
    "availability": 0.05,
}


@dataclass(slots=True, frozen=True)
class PriorityBreakdown:
    """Приоритет кандидата с объяснением по факторам.

    Сумма вкладов — от 0 до 100. Блокер не «вычитает», а запрещает
    автоматическое приглашение: он вынесен в отдельное поле.
    """

    percent: float
    coverage: float
    freshness: float
    availability: float
    weights: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_PRIORITY_WEIGHTS))
    has_blocker: bool = False

    @property
    def score(self) -> float:
        total = (
            self.weights.get("percent", 0.0) * self.percent
            + self.weights.get("coverage", 0.0) * self.coverage * 100.0
            + self.weights.get("freshness", 0.0) * self.freshness
            + self.weights.get("availability", 0.0) * self.availability
        )
        return max(0.0, min(100.0, total))

    def contributions(self) -> dict[str, float]:
        """Вклад каждого фактора — для карточки «почему этот кандидат первый»."""
        return {
            "percent": self.weights.get("percent", 0.0) * self.percent,
            "coverage": self.weights.get("coverage", 0.0) * self.coverage * 100.0,
            "freshness": self.weights.get("freshness", 0.0) * self.freshness,
            "availability": self.weights.get("availability", 0.0) * self.availability,
        }


def freshness_score(age_hours: float, *, half_life_hours: float = 24.0) -> float:
    """Свежесть отклика в шкале 0…100 (полураспад — сутки).

    Линейное «чем новее, тем лучше» плохо: отклик часовой и пятиминутный
    практически равны, а суточный уже сильно хуже. Экспонента это отражает.
    """
    if age_hours <= 0:
        return 100.0
    value = 100.0 * (0.5 ** (age_hours / half_life_hours))
    return float(max(0.0, value))


def availability_score(*, ready_days: int | None) -> float:
    """Готовность выйти на работу в шкале 0…100.

    Неизвестная готовность — не хуже известной: возвращаем середину, чтобы
    отсутствие данных не наказывало кандидата.
    """
    if ready_days is None:
        return 50.0
    if ready_days <= 0:
        return 100.0
    return max(0.0, 100.0 - min(ready_days, 30) * (100.0 / 30.0))


# --------------------------------------------------------------------------- #
# План вопросов
# --------------------------------------------------------------------------- #


@dataclass(slots=True, frozen=True)
class PlannedQuestion:
    """Вопрос, выбранный для конкретного кандидата."""

    question: ProfileQuestion
    criterion_key: str
    value: float
    reason: str = ""


@dataclass(slots=True)
class QuestionPlan:
    """Результат планирования вопросов."""

    questions: list[PlannedQuestion] = field(default_factory=list)
    stop_reason: str = ""
    detail: str = ""


def question_value(
    question: ProfileQuestion,
    criterion: ProfileCriterion,
    *,
    coverage: float,
    weights: dict[str, float],
) -> float:
    """Ценность вопроса: сколько информации он даёт за одно касание.

    Вопрос по обязательному критерию с нулевой уверенностью выигрывает у трёх
    вопросов по второстепенным, где данные уже есть.
    """
    uncertainty = 1.0 - max(0.0, min(1.0, coverage))
    must_have_bonus = 1.5 if criterion.must_have else 1.0
    weight_share = weights.get(criterion.key, 0.0) or (criterion.weight / MAX_WEIGHT) * 0.2
    return question.informativeness * must_have_bonus * uncertainty * (0.5 + weight_share)


@dataclass(slots=True, frozen=True)
class DecisionOutcome:
    """Итог решения по кандидату с объяснением."""

    decision: DecisionKind
    percent: float
    coverage: float
    blockers: tuple[str, ...]
    questions_asked: int = 0
    stop_reason: str = ""
    explanation: str = ""

    @property
    def requires_human(self) -> bool:
        return self.decision is DecisionKind.HUMAN


__all__ = [
    "DEFAULT_PRIORITY_WEIGHTS",
    "MAX_QUESTIONS",
    "MAX_WEIGHT",
    "MIN_QUESTIONS",
    "MIN_WEIGHT",
    "CriterionScore",
    "DecisionOutcome",
    "PlannedQuestion",
    "PriorityBreakdown",
    "ProfileCriterion",
    "ProfileQuestion",
    "ProfileThresholds",
    "QuestionPlan",
    "ScoringBreakdown",
    "ScoringProfile",
    "apply_must_have_cap",
    "availability_score",
    "compute_breakdown",
    "freshness_score",
    "normalized_weights",
    "question_value",
]
