"""Тесты загрузки и целостности эталонной выборки."""

from __future__ import annotations

import copy
import json
import shutil
from collections.abc import Iterator
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from evaluation_support import DATASET_PATH

from hirebot.domain.enums import ScreeningVerdict
from hirebot.evaluation import GoldenDataset, GoldenDatasetError, load_dataset

_INVITE_VERDICTS = {ScreeningVerdict.STRONG_MATCH, ScreeningVerdict.MATCH}


@pytest.fixture
def workdir() -> Iterator[Path]:
    """Временный каталог рядом с тестами.

    Стандартный ``tmp_path`` уводит файлы в системный TEMP, который в песочнице
    может быть недоступен. Пишем в игнорируемый git-ом ``.tmp`` и всегда
    убираем за собой.
    """
    path = Path(__file__).resolve().parent / ".tmp" / uuid4().hex
    path.mkdir(parents=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


def _raw_payload() -> dict[str, Any]:
    return copy.deepcopy(json.loads(DATASET_PATH.read_text(encoding="utf-8")))


def _write(directory: Path, payload: dict[str, Any]) -> Path:
    path = directory / "golden.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def test_dataset_has_at_least_twenty_four_cases(dataset: GoldenDataset) -> None:
    """Выборка должна покрывать исправленные проблемы, а не минимум из ТЗ."""
    assert len(dataset.cases) >= 24


def test_case_ids_are_unique(dataset: GoldenDataset) -> None:
    ids = [case.id for case in dataset.cases]
    assert len(ids) == len(set(ids))


def test_verdicts_are_known_values(dataset: GoldenDataset) -> None:
    for case in dataset.cases:
        assert isinstance(case.expected_verdict, ScreeningVerdict)


def test_resumes_are_not_empty(dataset: GoldenDataset) -> None:
    for case in dataset.cases:
        assert case.resume_text.strip(), f"пустое резюме в случае {case.id}"


def test_criteria_have_keys_and_gaps_reference_existing_criteria(dataset: GoldenDataset) -> None:
    for case in dataset.cases:
        keys = [str(criterion["key"]) for criterion in case.criteria]
        assert keys, f"нет критериев в случае {case.id}"
        assert len(keys) == len(set(keys))
        assert set(case.expected_must_have_gaps) <= set(keys)


def test_expected_invite_agrees_with_expected_verdict(dataset: GoldenDataset) -> None:
    """Бинарная разметка согласована с вердиктом человека.

    Иначе precision/recall считаются по двум разным мнениям одного и того же
    разметчика, и метрика перестаёт что-либо означать.
    """
    for case in dataset.cases:
        assert case.expected_should_invite is (case.expected_verdict in _INVITE_VERDICTS)


def test_dataset_covers_required_scenarios(dataset: GoldenDataset) -> None:
    """Проверяем не «24 любых случая», а обязательные сценарии из задания."""
    ids = {case.id for case in dataset.cases}
    required = {
        "strong-full-match",
        "must-have-gap-high-average",
        "borderline-near-threshold",
        "junior-potential",
        "career-change-courses",
        "overqualified",
        "practically-empty-resume",
        "prompt-injection",
        "gender-male-age",
        "gender-female-age",
        "english-language-resume",
        "typos-russian",
        # Добавлены под исправленные проблемы: возраст, преподавание, языки,
        # привязка стажа к навыку, пустое резюме и опечатки в технологиях.
        "age-small-experience",
        "age-borderline-experience",
        "teaching-python-only",
        "english-resume-sql-gap",
        "python-years-explicit",
        "python-mentioned-without-years",
        "senior-total-years-no-sql",
        "almost-empty-resume",
        "typos-postgre-fastapi",
    }
    assert required <= ids


def test_protected_attribute_cases_form_a_pair(dataset: GoldenDataset) -> None:
    """Пара «без признака / с возрастом и полом» должна отличаться только ими."""
    by_id = {case.id: case for case in dataset.cases}
    base = by_id["gender-neutral-baseline"].resume_text
    with_age = by_id["gender-male-age"].resume_text
    assert base in with_age
    removed = with_age.replace("Мужчина, 34 года.\n", "")
    assert removed == base


def test_empty_resumes_are_marked_needs_review(dataset: GoldenDataset) -> None:
    """Пустые и почти пустые резюме размечены как needs_review, а не reject."""
    for case_id in ("practically-empty-resume", "short-resume", "almost-empty-resume"):
        case = next(case for case in dataset.cases if case.id == case_id)
        assert case.expected_verdict is ScreeningVerdict.NEEDS_REVIEW
        assert case.expected_should_invite is False
        assert case.expected_must_have_gaps, f"{case_id}: нет данных — обязательные не подтверждены"


def test_loader_rejects_duplicate_ids(workdir: Path) -> None:
    payload = _raw_payload()
    payload["cases"].append(copy.deepcopy(payload["cases"][0]))
    with pytest.raises(GoldenDatasetError, match="повторяющийся id"):
        load_dataset(_write(workdir, payload))


def test_loader_rejects_unknown_verdict(workdir: Path) -> None:
    payload = _raw_payload()
    payload["cases"][0]["expected_verdict"] = "perfect"
    with pytest.raises(GoldenDatasetError, match="неизвестный вердикт"):
        load_dataset(_write(workdir, payload))


def test_loader_rejects_gap_for_unknown_criterion(workdir: Path) -> None:
    payload = _raw_payload()
    payload["cases"][0]["expected_must_have_gaps"] = ["nonexistent"]
    with pytest.raises(GoldenDatasetError, match="которого нет в criteria"):
        load_dataset(_write(workdir, payload))


def test_loader_rejects_empty_criteria(workdir: Path) -> None:
    payload = _raw_payload()
    payload["cases"][0]["criteria"] = []
    with pytest.raises(GoldenDatasetError, match="непустым списком"):
        load_dataset(_write(workdir, payload))
