"""Тесты эвристики fake-модели: она не должна выдавать возраст за стаж,
упоминание за опыт, а учебные проекты — за коммерческую разработку.

Заглушка — это нижняя граница замера (см. ``docs/EVALUATION.md``), но её
правила обязаны быть честными: иначе harness проверяет неверные оценки, а
регрессии в промпте/парсере остаются незамеченными.
"""

from __future__ import annotations

from typing import Any

import pytest

from hirebot.adapters.llm.fake import FakeLlmAdapter
from hirebot.domain.entities import ScreeningCriterion
from hirebot.ports.llm import ScreeningDraft, ScreeningRequest

CRITERIA: tuple[dict[str, Any], ...] = (
    {
        "key": "python",
        "title": "Коммерческая разработка на Python",
        "weight": 3.0,
        "must_have": True,
        "expectation": "не менее 3 лет коммерческой разработки на Python",
    },
    {
        "key": "sql",
        "title": "Работа с реляционными базами данных и SQL",
        "weight": 2.0,
        "must_have": True,
        "expectation": "уверенное владение SQL и опыт работы с PostgreSQL",
    },
    {
        "key": "api",
        "title": "Проектирование REST API",
        "weight": 2.0,
        "must_have": False,
        "expectation": "опыт проектирования и поддержки REST API",
    },
    {
        "key": "docker",
        "title": "Docker и контейнеризация",
        "weight": 1.0,
        "must_have": False,
        "expectation": "практический опыт работы с Docker",
    },
    {
        "key": "english",
        "title": "Английский язык для документации",
        "weight": 1.0,
        "must_have": False,
        "expectation": "английский язык не ниже B1",
    },
)


async def screen(resume_text: str) -> ScreeningDraft:
    """Прогнать резюме через fake-модель как настоящий LlmPort."""
    adapter = FakeLlmAdapter()
    return await adapter.screen_resume(
        ScreeningRequest(
            vacancy_title="Python-разработчик (middle)",
            vacancy_description="FastAPI, REST API, PostgreSQL",
            criteria=CRITERIA,
            resume_text=resume_text,
            content_hash="test-hash",
        )
    )


def criterion(draft: ScreeningDraft, key: str) -> ScreeningCriterion:
    return next(item for item in draft.criteria if item.key == key)


_BASE = (
    "Backend-разработчик.\n"
    "Опыт работы: 4 года коммерческой разработки на Python.\n"
    "Писал сервисы на Python и FastAPI.\n"
    "Работал с PostgreSQL, писал SQL-запросы.\n"
    "Docker: запускал контейнеры.\n"
    "Английский язык — B1."
)


async def test_age_does_not_change_score() -> None:
    """Два одинаковых резюме, отличающихся только возрастом, оцениваются одинаково."""
    baseline = await screen(_BASE)
    with_age = await screen("Мужчина, 34 года.\n" + _BASE)

    assert baseline.total_score == with_age.total_score
    assert [item.score for item in baseline.criteria] == [item.score for item in with_age.criteria]


async def test_age_is_not_experience() -> None:
    """«34 года» при одном годе работы не должны закрывать требование 3 лет."""
    draft = await screen(
        "Мужчина, 34 года.\n"
        "Backend-разработчик.\n"
        "Опыт работы: 1 год коммерческой разработки на Python.\n"
        "Работал с PostgreSQL, писал SQL-запросы.\n"
        "Английский язык — B1."
    )
    python = criterion(draft, "python")
    assert python.score < 55.0
    assert python.is_gap is True
    assert criterion(draft, "sql").is_gap is False


async def test_birth_year_is_not_experience() -> None:
    """«1989 г.р.» — не стаж: годы берутся только из конструкций опыта."""
    draft = await screen(
        "1989 г.р.\nОпыт работы: 1 год коммерческой разработки на Python.\nАнглийский язык — B1."
    )
    assert criterion(draft, "python").is_gap is True


async def test_mentioning_skill_does_not_confirm_experience() -> None:
    """«Знаком с SQL» — упоминание, а не опыт работы с базами данных."""
    draft = await screen("Python. Знаком с SQL, читал документацию. Английский B1.")
    assert criterion(draft, "sql").is_gap is True
    assert criterion(draft, "sql").score < 55.0


async def test_teaching_and_courses_do_not_confirm_commercial_experience() -> None:
    """Преподавание и курсы не заменяют 3 года коммерческой разработки."""
    draft = await screen(
        "Опыт работы: 8 лет — преподаватель информатики в школе.\n"
        "Преподавал Python и SQL, вёл кружок программирования.\n"
        "Прошёл курсы backend-разработки: Python, Django, PostgreSQL.\n"
        "Английский язык — B1."
    )
    python = criterion(draft, "python")
    assert python.is_gap is True
    assert "преподав" in python.evidence.casefold() or "курс" in python.evidence.casefold()


async def test_educational_projects_do_not_confirm_commercial_experience() -> None:
    draft = await screen(
        "Опыт работы: 10 лет — преподаватель математики.\n"
        "Прошла курсы Python-разработки: Python, Django, SQL, Docker.\n"
        "Учебные проекты: телеграм-бот на Python, REST API на Django, PostgreSQL.\n"
        "Английский язык — B2."
    )
    assert criterion(draft, "python").is_gap is True
    # SQL/PostgreSQL как навык в учебных проектах подтверждён, но не как стаж.
    assert criterion(draft, "sql").is_gap is False


async def test_english_technology_forms_are_recognized() -> None:
    """«PostgreSQL databases» и «English (C1)» — это SQL и английский."""
    draft = await screen(
        "Alex Ivanov — Backend Developer\n"
        "Experience: 6 years of commercial software development.\n"
        "Built and maintained services in Python (FastAPI, Django).\n"
        "Designed REST API and worked with PostgreSQL databases.\n"
        "Packaged services with Docker and ran them in CI/CD.\n"
        "Fluent English (C1), read technical documentation daily."
    )
    for key in ("python", "sql", "api", "docker", "english"):
        item = criterion(draft, key)
        assert item.score >= 80.0, f"{key}: английская форма не распознана"
        assert item.is_gap is False


async def test_skill_linked_years_close_criterion() -> None:
    """«Python (5 years)» — стаж привязан к навыку, критерий закрыт."""
    draft = await screen(
        "Backend-разработчик. Python (5 years), SQL (4 years).\n"
        "Опыт работы: 5 лет — backend-разработчик.\n"
        "Проектировал REST API, работал с PostgreSQL.\n"
        "Английский язык — B2."
    )
    python = criterion(draft, "python")
    assert python.is_gap is False
    assert python.score >= 90.0


async def test_skill_without_years_does_not_close_experience_criterion() -> None:
    """«Python» без стажа — только упоминание: требование 3 лет не закрыто."""
    draft = await screen("Backend Developer.\nPython, FastAPI, REST API, PostgreSQL, Docker.\nEnglish B2.")
    python = criterion(draft, "python")
    assert python.is_gap is True
    assert python.score < 55.0


async def test_technology_typos_and_spacing_are_recognized() -> None:
    """«Postgre SQL», «Fast Api», «Pythom» — опечатки, а не другие технологии."""
    draft = await screen(
        "Опыт работы: 4 года коммерческой разработки на Pythom.\n"
        "Писал сервисы на Fast Api и Pyhton, проектировал REST API.\n"
        "Работал с Postgre SQL, писал запросы.\n"
        "Docker — запускал контейнеры.\n"
        "Английский язык — B1."
    )
    for key in ("python", "sql", "api"):
        assert criterion(draft, key).is_gap is False, f"{key}: опечатка не распознана"


async def test_must_have_gaps_are_reported_without_commercial_experience() -> None:
    draft = await screen("Продавец-консультант. Опыт работы: 1 год в розничной торговле.")
    assert criterion(draft, "python").is_gap is True
    assert criterion(draft, "sql").is_gap is True


async def test_model_name_reflects_heuristic_version() -> None:
    adapter = FakeLlmAdapter()
    draft = await adapter.screen_resume(
        ScreeningRequest(
            vacancy_title="Python-разработчик",
            vacancy_description="",
            criteria=CRITERIA,
            resume_text=_BASE,
            content_hash="test-hash",
        )
    )
    assert draft.model == "fake-llm-v2"
    assert adapter.model_name == "fake-llm-v2"


@pytest.mark.parametrize("age_line", ["Мужчина, 34 года.", "Женщина, 29 лет.", "1989 г.р."])
async def test_protected_lines_do_not_change_any_criterion(age_line: str) -> None:
    baseline = await screen(_BASE)
    with_line = await screen(age_line + "\n" + _BASE)
    assert [item.score for item in baseline.criteria] == [item.score for item in with_line.criteria]
