"""Тесты каталога критериев.

Каталог — это данные, которые правит человек (в том числе вместе с
руководителем), поэтому проверяется:

* каталог в репозитории корректен и уникален по ключам;
* в нём **нет запрещённых признаков** — загрузка прогоняется через тот же
  PolicyGuard, что и рабочая настройка вакансии;
* выбор критериев по ключам даёт ровно тот формат, который ждёт вакансия;
* ошибки в данных дают понятное сообщение, а не падение.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from hirebot.criteria import (
    CRITERION_KINDS,
    MEASURABLE_LEVELS,
    CatalogError,
    load_catalog,
    render_markdown,
    selected_criteria,
)
from hirebot.domain.errors import PolicyViolation


def test_repository_catalog_is_valid() -> None:
    """Каталог из репозитория проходит полную проверку."""
    catalog = load_catalog()

    assert len(catalog.criteria) >= 30, "каталог должен покрывать основные блоки подбора"
    assert len(catalog.categories) >= 6
    assert catalog.forbidden, "раздел «чего нельзя» обязателен"


def test_all_criteria_have_valid_fields() -> None:
    catalog = load_catalog()

    keys = [item.key for item in catalog.criteria]
    assert len(keys) == len(set(keys)), "ключи критериев должны быть уникальны"

    for item in catalog.criteria:
        assert item.category in catalog.categories
        assert item.kind in CRITERION_KINDS
        assert item.measurable in MEASURABLE_LEVELS
        assert 1 <= item.weight_hint <= 3
        assert item.title.strip()
        assert item.question.strip(), f"у {item.key} нет формулировки вопроса"


def test_catalog_contains_no_forbidden_criteria() -> None:
    """Главная проверка: в справочнике не может быть дискриминационных критериев.

    Загрузка уже прогоняет каждый критерий через PolicyGuard, поэтому здесь
    достаточно убедиться, что она прошла и запрещённые слова не просочились
    через заголовки.
    """
    catalog = load_catalog()

    forbidden_markers = ("возраст", "пол ", "национальн", "прописк", "семейное", "религи")
    for item in catalog.criteria:
        probe = f"{item.title} {item.question}".casefold()
        for marker in forbidden_markers:
            assert marker not in probe, f"критерий {item.key} похож на запрещённый: {marker}"


def test_catalog_rejects_forbidden_criterion() -> None:
    """Если кто-то добавит «возраст до 40», загрузка должна упасть.

    Файл пишется в каталог проекта, а не в ``tmp_path``: в ограниченных
    окружениях системный TEMP может быть недоступен для записи.
    """
    bad = Path(".tmp") / "bad_catalog.yaml"
    bad.parent.mkdir(parents=True, exist_ok=True)
    bad.write_text(
        """
categories:
  experience: {title: Опыт}
criteria:
  - key: age_limit
    title: Возраст до 40 лет
    category: experience
    kind: experience
    question: Сколько лет кандидату?
    weight_hint: 2
""",
        encoding="utf-8",
    )
    try:
        with pytest.raises(PolicyViolation):
            load_catalog(bad)
    finally:
        bad.unlink(missing_ok=True)


def test_selected_criteria_match_vacancy_format() -> None:
    """Выбранные ключи превращаются в содержимое ``vacancies.criteria``."""
    catalog = load_catalog()

    chosen = selected_criteria(catalog, ["python", "sql"])

    assert len(chosen) == 2
    assert chosen[0] == {
        "key": "python",
        "title": catalog.get("python").title,
        "weight": 3.0,
        "must_have": True,
    }
    assert isinstance(chosen[0]["weight"], float)
    assert isinstance(chosen[0]["must_have"], bool)


def test_selected_criteria_reports_unknown_key() -> None:
    """Опечатка в ключе — ошибка, а не молчаливое «критерий не выбран»."""
    catalog = load_catalog()

    with pytest.raises(CatalogError, match="нет критериев"):
        selected_criteria(catalog, ["python", "pythom"])


def test_criteria_with_legal_notes_are_flagged() -> None:
    """Юридически чувствительные критерии помечены и не теряются."""
    catalog = load_catalog()

    legal = catalog.with_legal_notes()

    assert legal, "критерии, требующие юриста, должны быть помечены"
    assert any(item.key == "work_permit" for item in legal)
    assert all(item.legal_note for item in legal)


def test_markdown_document_is_generated() -> None:
    """Документ собирается из данных: он не может разойтись с каталогом."""
    catalog = load_catalog()

    text = render_markdown(catalog)

    assert "Каталог критериев" in text
    assert "Чего в каталоге нет и почему" in text
    for category in catalog.categories.values():
        if any(item.category == category.key for item in catalog.criteria):
            assert category.title in text
    # Все ключи попадают в документ: пропуск означал бы потерю критерия.
    for item in catalog.criteria:
        assert f"`{item.key}`" in text


def test_missing_catalog_file_raises() -> None:
    with pytest.raises(CatalogError, match="не найден"):
        load_catalog("data/criteria/does-not-exist.yaml")


def test_catalog_document_in_repo_is_up_to_date() -> None:
    """Сгенерированный документ совпадает с данными.

    Иначе правка YAML оставила бы в репозитории устаревший справочник, по
    которому руководитель выбирал бы критерии.
    """
    catalog = load_catalog()
    document = Path("docs/CRITERIA-CATALOG.md")

    assert document.is_file(), "документ каталога должен лежать в docs/"
    # Сравниваем после нормализации переносов: файл мог быть записан на Windows.
    stored = document.read_text(encoding="utf-8").replace("\r\n", "\n").strip()
    expected = render_markdown(catalog).replace("\r\n", "\n").strip()
    assert stored == expected, (
        "docs/CRITERIA-CATALOG.md устарел: перегенерируйте "
        "`hirebot criteria --markdown > docs/CRITERIA-CATALOG.md`"
    )
