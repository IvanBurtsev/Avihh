"""Эталонная выборка для замера качества скрининга.

Выборка — это JSON-файл с вакансией и набором ``GoldenCase``. Каждый случай
содержит резюме, критерии вакансии и **независимую разметку человека**:
ожидаемый вердикт, бинарное решение «приглашать / не приглашать» и список
обязательных критериев, которые должны быть признаны проваленными.

Разметка не выводится из ответа модели. Иначе замер превращается в проверку
«модель согласна сама с собой» и не отвечает на вопрос из
``docs/ARCHITECTURE.md``: «точность промптов не измерена на размеченной
выборке». Поэтому ``expected_*`` заполняется по смыслу резюме относительно
критериев, а не по текущему выводу fake-провайдера.

Формат файла::

    {
      "version": "1.0.0",
      "vacancy_title": "...",
      "vacancy_description": "...",
      "cases": [
        {
          "id": "unique-case-id",
          "title": "...",
          "resume_text": "...",
          "criteria": [{"key": "python", "title": "...", ...}],
          "expected_verdict": "match",
          "expected_should_invite": true,
          "expected_must_have_gaps": [],
          "notes": "почему разметка именно такая"
        }
      ]
    }

Загрузчик намеренно строгий: дубли ``id``, неизвестный вердикт, пустой список
критериев и ссылка на несуществующий ключ критерия в ``expected_must_have_gaps``
считаются ошибкой выборки, а не «мелочью». Иначе метрики посчитаются по
испорченным данным и никто этого не заметит.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from hirebot.domain.enums import ScreeningVerdict

__all__ = [
    "GoldenCase",
    "GoldenDataset",
    "GoldenDatasetError",
    "load_dataset",
]


class GoldenDatasetError(ValueError):
    """Ошибка формата или содержания эталонной выборки."""


@dataclass(frozen=True, slots=True)
class GoldenCase:
    """Один размеченный случай: резюме + критерии + ожидания человека."""

    id: str
    title: str
    resume_text: str
    criteria: tuple[dict[str, Any], ...]
    #: Независимая разметка человека, а не вывод модели.
    expected_verdict: ScreeningVerdict
    #: Бинарное бизнес-решение: приглашать кандидата или нет.
    expected_should_invite: bool
    #: Ключи обязательных критериев, которые в этом резюме обязаны быть провалены.
    expected_must_have_gaps: tuple[str, ...]
    notes: str = ""


@dataclass(frozen=True, slots=True)
class GoldenDataset:
    """Вакансия и набор размеченных случаев для одного прогона замера."""

    version: str
    vacancy_title: str
    vacancy_description: str
    cases: tuple[GoldenCase, ...]


def load_dataset(path: str | Path) -> GoldenDataset:
    """Прочитать и провалидировать эталонную выборку из JSON.

    Ошибки чтения, JSON и схемы оборачиваются в ``GoldenDatasetError`` с
    указанием места: по сообщению должно быть понятно, какой именно случай
    сломан, без открытия отладчика.
    """
    source = Path(path)
    try:
        text = source.read_text(encoding="utf-8")
    except OSError as exc:
        msg = f"Не удалось прочитать эталонную выборку {source}: {exc}"
        raise GoldenDatasetError(msg) from exc

    try:
        raw: object = json.loads(text)
    except json.JSONDecodeError as exc:
        msg = f"{source}: некорректный JSON ({exc.msg}, строка {exc.lineno})"
        raise GoldenDatasetError(msg) from exc

    data = _as_mapping(raw, where=str(source))
    version = _as_str(data, "version", where=str(source))
    vacancy_title = _as_str(data, "vacancy_title", where=str(source))
    vacancy_description = _as_str(
        data,
        "vacancy_description",
        where=str(source),
        allow_empty=True,
    )

    raw_cases = data.get("cases")
    if not isinstance(raw_cases, list) or not raw_cases:
        msg = f"{source}: поле 'cases' должно быть непустым списком"
        raise GoldenDatasetError(msg)

    cases: list[GoldenCase] = []
    seen_ids: set[str] = set()
    for index, raw_case in enumerate(raw_cases, start=1):
        case = _parse_case(raw_case, where=f"cases[{index}]")
        if case.id in seen_ids:
            msg = f"{source}: повторяющийся id случая {case.id!r}"
            raise GoldenDatasetError(msg)
        seen_ids.add(case.id)
        cases.append(case)

    return GoldenDataset(
        version=version,
        vacancy_title=vacancy_title,
        vacancy_description=vacancy_description,
        cases=tuple(cases),
    )


def _parse_case(raw: object, *, where: str) -> GoldenCase:
    case = _as_mapping(raw, where=where)
    case_id = _as_str(case, "id", where=where)
    title = _as_str(case, "title", where=where)
    resume_text = _as_str(case, "resume_text", where=where, allow_empty=True)
    criteria = _parse_criteria(case.get("criteria"), where=where)
    criteria_keys = {str(criterion["key"]) for criterion in criteria}

    raw_verdict = case.get("expected_verdict")
    if not isinstance(raw_verdict, str):
        msg = f"{where}: поле 'expected_verdict' должно быть строкой"
        raise GoldenDatasetError(msg)
    try:
        expected_verdict = ScreeningVerdict(raw_verdict)
    except ValueError as exc:
        allowed = ", ".join(item.value for item in ScreeningVerdict)
        msg = f"{where}: неизвестный вердикт {raw_verdict!r}; допустимые: {allowed}"
        raise GoldenDatasetError(msg) from exc

    raw_invite = case.get("expected_should_invite")
    if not isinstance(raw_invite, bool):
        msg = f"{where}: поле 'expected_should_invite' должно быть true/false"
        raise GoldenDatasetError(msg)

    raw_gaps = case.get("expected_must_have_gaps")
    if not isinstance(raw_gaps, list):
        msg = f"{where}: поле 'expected_must_have_gaps' должно быть списком"
        raise GoldenDatasetError(msg)
    gaps: list[str] = []
    for position, gap in enumerate(raw_gaps, start=1):
        if not isinstance(gap, str) or not gap:
            msg = f"{where}: expected_must_have_gaps[{position}] должен быть непустой строкой"
            raise GoldenDatasetError(msg)
        if gap not in criteria_keys:
            msg = (
                f"{where}: expected_must_have_gaps содержит ключ {gap!r}, "
                f"которого нет в criteria ({sorted(criteria_keys)})"
            )
            raise GoldenDatasetError(msg)
        if gap in gaps:
            msg = f"{where}: ключ {gap!r} повторяется в expected_must_have_gaps"
            raise GoldenDatasetError(msg)
        gaps.append(gap)

    notes = case.get("notes", "")
    if not isinstance(notes, str):
        msg = f"{where}: поле 'notes' должно быть строкой"
        raise GoldenDatasetError(msg)

    return GoldenCase(
        id=case_id,
        title=title,
        resume_text=resume_text,
        criteria=criteria,
        expected_verdict=expected_verdict,
        expected_should_invite=raw_invite,
        expected_must_have_gaps=tuple(gaps),
        notes=notes,
    )


def _parse_criteria(raw: object, *, where: str) -> tuple[dict[str, Any], ...]:
    if not isinstance(raw, list) or not raw:
        msg = f"{where}: поле 'criteria' должно быть непустым списком"
        raise GoldenDatasetError(msg)

    criteria: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for position, item in enumerate(raw, start=1):
        criterion = _as_mapping(item, where=f"{where}.criteria[{position}]")
        key = _as_str(criterion, "key", where=f"{where}.criteria[{position}]")
        if key in seen_keys:
            msg = f"{where}: ключ критерия {key!r} повторяется"
            raise GoldenDatasetError(msg)
        seen_keys.add(key)
        criteria.append(dict(criterion))
    return tuple(criteria)


def _as_mapping(value: object, *, where: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        msg = f"{where}: ожидался JSON-объект, получено {type(value).__name__}"
        raise GoldenDatasetError(msg)
    return cast(dict[str, Any], value)


def _as_str(
    mapping: Mapping[str, Any],
    key: str,
    *,
    where: str,
    allow_empty: bool = False,
) -> str:
    value = mapping.get(key)
    if not isinstance(value, str):
        msg = f"{where}: поле {key!r} должно быть строкой"
        raise GoldenDatasetError(msg)
    if not allow_empty and not value.strip():
        msg = f"{where}: поле {key!r} не может быть пустым"
        raise GoldenDatasetError(msg)
    return value
