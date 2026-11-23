"""Разбор формы настройки критериев и подготовка данных для экрана.

Зачем отдельный модуль: роутер, который и читает форму, и проверяет данные, и
собирает представление, быстро становится нечитаемым. Здесь три чистые функции
без HTTP и без шаблонов — их можно проверить тестом напрямую, без клиента.

Разбор формы устойчив к «грязному» вводу: HTML-форма приходит строками, часть
полей может отсутствовать, числа могут быть нечисловыми. Некорректное значение
не роняет запрос — оно заменяется значением по умолчанию в допустимых границах,
а смысловые ошибки (нет критериев, нет обязательного) проверяются доменом.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

from hirebot.criteria import Catalog
from hirebot.domain.enums import QuestionKind
from hirebot.domain.scoring import (
    MAX_QUESTIONS,
    MIN_WEIGHT,
    ProfileCriterion,
    ProfileQuestion,
    ProfileThresholds,
)

#: Сколько слотов под вопросы показывает форма. Совпадает с жёстким лимитом.
QUESTION_SLOTS = MAX_QUESTIONS

#: Значения порогов по умолчанию — те же, что в домене.
DEFAULT_INVITE_AT = 85
DEFAULT_HUMAN_AT = 55
DEFAULT_REJECT_AT = 40
DEFAULT_MIN_COVERAGE = 70


@dataclass(slots=True)
class ScoringFormData:
    """Результат разбора формы."""

    criteria: list[ProfileCriterion] = field(default_factory=list)
    questions: list[ProfileQuestion] = field(default_factory=list)
    thresholds: ProfileThresholds = field(default_factory=ProfileThresholds)
    errors: list[str] = field(default_factory=list)

    @property
    def is_valid(self) -> bool:
        return not self.errors


def _int_or(value: object, default: int, *, low: int, high: int) -> int:
    """Целое в границах; некорректный ввод не должен ломать запрос."""
    try:
        parsed = int(str(value))
    except (TypeError, ValueError):
        return default
    return max(low, min(high, parsed))


def parse_scoring_form(form: Mapping[str, Any], catalog: Catalog) -> ScoringFormData:
    """Собрать критерии, вопросы и пороги из данных формы.

    Критерии каталога приходят только отмеченными: HTML не отправляет
    невыбранные чекбоксы, поэтому «выбрано» = «есть поле ``selected_<key>``».
    Свой критерий добавляется, только если указаны и название, и что искать:
    без второго модель не сможет его оценить.
    """
    result = ScoringFormData()
    by_key = {item.key: item for item in catalog.criteria}

    position = 0
    for key, known in by_key.items():
        if form.get(f"selected_{key}") is None:
            continue
        weight = _int_or(form.get(f"weight_{key}"), known.weight_hint, low=1, high=5)
        result.criteria.append(
            ProfileCriterion(
                key=key,
                title=known.title,
                weight=weight,
                must_have=form.get(f"must_{key}") is not None,
                evidence_hint=known.evidence_hint,
                expectation=known.expectation or known.title,
                source="catalog",
                position=position,
            )
        )
        position += 1

    custom_title = str(form.get("custom_title") or "").strip()
    custom_evidence = str(form.get("custom_evidence") or "").strip()
    if custom_title and custom_evidence:
        result.criteria.append(
            ProfileCriterion(
                key=f"custom_{position}",
                title=custom_title,
                weight=_int_or(form.get("custom_weight"), 3, low=MIN_WEIGHT, high=5),
                must_have=form.get("custom_must") is not None,
                evidence_hint=custom_evidence,
                expectation=custom_title,
                source="custom",
                position=position,
            )
        )
    elif custom_title and not custom_evidence:
        result.errors.append(
            "У своего критерия не указано, что искать в резюме — без этого он не будет оценён"
        )

    keys = {item.key for item in result.criteria}
    for index in range(QUESTION_SLOTS):
        text = str(form.get(f"question_text_{index}") or "").strip()
        criterion_key = str(form.get(f"question_criterion_{index}") or "").strip()
        if not text and not criterion_key:
            continue
        if not text:
            result.errors.append(f"Вопрос №{index + 1}: не заполнен текст")
            continue
        if not criterion_key:
            result.errors.append(f"Вопрос №{index + 1}: не выбран критерий")
            continue
        if criterion_key not in keys:
            result.errors.append(f"Вопрос №{index + 1} ссылается на критерий, который не выбран")
            continue
        result.questions.append(
            ProfileQuestion(
                text=text,
                criterion_key=criterion_key,
                kind=QuestionKind.FACT,
                informativeness=2,
                is_custom=True,
                position=index,
            )
        )

    if len(result.questions) > MAX_QUESTIONS:  # pragma: no cover - слотов ровно 5
        result.questions = result.questions[:MAX_QUESTIONS]

    invite_at = float(_int_or(form.get("invite_at"), DEFAULT_INVITE_AT, low=0, high=100))
    human_at = float(_int_or(form.get("human_at"), DEFAULT_HUMAN_AT, low=0, high=100))
    reject_at = float(_int_or(form.get("reject_at"), DEFAULT_REJECT_AT, low=0, high=100))
    try:
        result.thresholds = ProfileThresholds(
            invite_at=invite_at,
            human_at=human_at,
            reject_at=reject_at,
            auto_invite=form.get("auto_invite") is not None,
            min_coverage_for_auto=_int_or(form.get("min_coverage"), DEFAULT_MIN_COVERAGE, low=0, high=100)
            / 100.0,
        )
    except ValueError:
        # Некорректный порядок порогов — это ошибка пользователя, а не сбой:
        # сообщаем словами и оставляем значения по умолчанию.
        result.thresholds = ProfileThresholds()
        result.errors.append("Пороги идут не по возрастанию: отказ ≤ «передать человеку» ≤ приглашение")

    if not result.criteria:
        result.errors.append("Выберите хотя бы один критерий")

    return result


def check_thresholds_order(data: ScoringFormData) -> str | None:
    """Проверить порядок порогов отдельно от конструктора.

    Конструктор ``ProfileThresholds`` падает на неверном порядке, но в ответе
    должно быть понятное объяснение, а не трассировка.
    """
    thresholds = data.thresholds
    if thresholds.reject_at > thresholds.human_at:
        return "Порог отказа не может быть выше порога «передать человеку»"
    if thresholds.human_at > thresholds.invite_at:
        return "Порог «передать человеку» не может быть выше порога приглашения"
    return None


def build_groups(catalog: Catalog, selected: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Сгруппировать каталог по категориям с отметками выбранного.

    Свои критерии (не из каталога) показываются отдельной группой, чтобы не
    терялись при повторном открытии экрана.
    """
    grouped = catalog.by_category()
    groups: list[dict[str, Any]] = []

    for category_key, category in catalog.categories.items():
        items = []
        for criterion in grouped.get(category_key, []):
            chosen = selected.get(criterion.key)
            items.append(
                {
                    "key": criterion.key,
                    "title": criterion.title,
                    "question": criterion.question,
                    "needs_lawyer": criterion.needs_lawyer,
                    "selected": chosen is not None,
                    "weight": chosen["weight"] if chosen else criterion.weight_hint,
                    "must_have": chosen["must_have"] if chosen else criterion.must_have_hint,
                }
            )
        if items:
            groups.append({"title": category.title, "criteria": items})

    known = {item.key for item in catalog.criteria}
    custom = [
        {
            "key": key,
            "title": value.get("title") or key,
            "question": value.get("expectation") or "",
            "needs_lawyer": False,
            "selected": True,
            "weight": value["weight"],
            "must_have": value["must_have"],
        }
        for key, value in selected.items()
        if key not in known
    ]
    if custom:
        groups.append({"title": "Свои критерии", "criteria": custom})

    return groups


__all__ = [
    "DEFAULT_HUMAN_AT",
    "DEFAULT_INVITE_AT",
    "DEFAULT_MIN_COVERAGE",
    "DEFAULT_REJECT_AT",
    "QUESTION_SLOTS",
    "ScoringFormData",
    "build_groups",
    "check_thresholds_order",
    "parse_scoring_form",
]
