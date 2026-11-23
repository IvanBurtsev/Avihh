"""Тесты экрана настройки критериев и разбора формы.

Проверяется то, что видит рекрутер и что может сломаться незаметно:

* выбраны только отмеченные критерии, вес и обязательность берутся из формы;
* свой критерий без «что искать» отклоняется — иначе модель его не оценит;
* вопрос без критерия или на невыбранный критерий не принимается;
* пороги проверяются на порядок с понятным сообщением;
* страницы отдают 200 и содержат три шага.

HTTP-клиент идёт через ASGI: сеть, БД и Redis не нужны.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any
from urllib.parse import unquote, unquote_plus
from uuid import UUID, uuid4

import httpx
import pytest

from hirebot.api.app import create_app
from hirebot.api.scoring_forms import (
    build_groups,
    check_thresholds_order,
    parse_scoring_form,
)
from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.criteria import load_catalog
from hirebot.domain.entities import Company, Vacancy
from hirebot.domain.enums import Channel
from hirebot.services.base import DEFAULT_COMPANY_ID

CATALOG_PATH = "data/criteria/catalog.yaml"


def _settings() -> Settings:
    return Settings(env="test", log_level="WARNING", log_json=False, _env_file=None)


@pytest.fixture
def catalog() -> Any:
    return load_catalog(CATALOG_PATH)


# ---------------------------------------------------------------- форма


def test_only_checked_criteria_are_taken(catalog: Any) -> None:
    form = {
        "selected_python": "1",
        "weight_python": "5",
        "must_python": "1",
        "selected_sql": "1",
        "weight_sql": "2",
        # english не отмечен — его не должно быть в результате
    }

    parsed = parse_scoring_form(form, catalog)

    keys = {item.key for item in parsed.criteria}
    assert keys == {"python", "sql"}
    python = next(item for item in parsed.criteria if item.key == "python")
    assert python.weight == 5
    assert python.must_have is True
    sql = next(item for item in parsed.criteria if item.key == "sql")
    assert sql.weight == 2
    assert sql.must_have is False


def test_expectation_is_preserved_from_catalog(catalog: Any) -> None:
    """Ожидание («не менее 3 лет») обязано дойти до модели: без него оценка размыта."""
    parsed = parse_scoring_form({"selected_python": "1"}, catalog)

    python = next(item for item in parsed.criteria if item.key == "python")
    assert python.expectation, "у критерия должно быть ожидание"
    assert python.evidence_hint, "и подсказка, что искать"


def test_no_criteria_is_an_error(catalog: Any) -> None:
    parsed = parse_scoring_form({}, catalog)

    assert not parsed.is_valid
    assert any("хотя бы один критерий" in error for error in parsed.errors)


def test_custom_criterion_requires_evidence_hint(catalog: Any) -> None:
    parsed = parse_scoring_form({"custom_title": "Опыт с Kafka", "custom_weight": "3"}, catalog)

    assert not parsed.is_valid
    assert any("что искать" in error for error in parsed.errors)


def test_custom_criterion_with_evidence_is_added(catalog: Any) -> None:
    parsed = parse_scoring_form(
        {
            "custom_title": "Опыт с Kafka",
            "custom_evidence": "Kafka, брокеры сообщений",
            "custom_weight": "4",
            "custom_must": "1",
        },
        catalog,
    )

    assert parsed.is_valid
    custom = next(item for item in parsed.criteria if item.source == "custom")
    assert custom.weight == 4
    assert custom.must_have is True
    assert custom.evidence_hint == "Kafka, брокеры сообщений"


def test_question_requires_selected_criterion(catalog: Any) -> None:
    parsed = parse_scoring_form(
        {
            "selected_python": "1",
            "question_text_0": "Сколько лет на Python?",
            "question_criterion_0": "sql",  # не выбран
        },
        catalog,
    )

    assert not parsed.is_valid
    assert any("не выбран" in error for error in parsed.errors)


def test_question_without_text_is_rejected(catalog: Any) -> None:
    parsed = parse_scoring_form({"selected_python": "1", "question_criterion_0": "python"}, catalog)

    assert not parsed.is_valid
    assert any("не заполнен текст" in error for error in parsed.errors)


def test_valid_questions_are_parsed(catalog: Any) -> None:
    parsed = parse_scoring_form(
        {
            "selected_python": "1",
            "selected_sql": "1",
            "question_text_0": "Сколько лет на Python?",
            "question_criterion_0": "python",
            "question_text_1": "Какие объёмы данных?",
            "question_criterion_1": "sql",
        },
        catalog,
    )

    assert parsed.is_valid
    assert len(parsed.questions) == 2
    assert {item.criterion_key for item in parsed.questions} == {"python", "sql"}


def test_thresholds_defaults_and_order(catalog: Any) -> None:
    parsed = parse_scoring_form({"selected_python": "1"}, catalog)

    assert parsed.thresholds.invite_at == 85.0
    assert parsed.thresholds.human_at == 55.0
    assert parsed.thresholds.reject_at == 40.0
    assert check_thresholds_order(parsed) is None


def test_invalid_threshold_numbers_fall_back_to_defaults(catalog: Any) -> None:
    """Мусор в поле не должен ронять запрос: берём значение по умолчанию.

    При этом некорректный порядок порогов — ошибка пользователя, и она должна
    попасть в список ошибок, а не превратиться в исключение.
    """
    parsed = parse_scoring_form(
        {"selected_python": "1", "invite_at": "не число", "human_at": "-999"}, catalog
    )

    assert parsed.thresholds.invite_at == 85.0
    assert not parsed.is_valid
    assert any("по возрастанию" in error for error in parsed.errors)


def test_groups_include_custom_criteria(catalog: Any) -> None:
    selected = {
        "python": {"weight": 5, "must_have": True, "title": "Python"},
        "custom_1": {"weight": 3, "must_have": False, "title": "Своё"},
    }

    groups = build_groups(catalog, selected)

    titles = [group["title"] for group in groups]
    assert "Свои критерии" in titles
    custom_group = next(group for group in groups if group["title"] == "Свои критерии")
    assert custom_group["criteria"][0]["key"] == "custom_1"


# ---------------------------------------------------------------- страницы


async def _client() -> AsyncIterator[httpx.AsyncClient]:
    container = build_fake_container(_settings(), seed_demo=False)
    vacancy = Vacancy(
        id=uuid4(),
        channel=Channel.HH,
        external_id="vac-ui-1",
        title="Python-разработчик",
        company_id=DEFAULT_COMPANY_ID,
        criteria=[{"key": "python", "title": "Python", "weight": 5.0, "must_have": True}],
    )
    async with container.uow_factory() as uow:
        await uow.companies.upsert(Company(id=DEFAULT_COMPANY_ID, name="Компания", slug="company-ui"))
        await uow.vacancies.upsert(vacancy)
        await uow.commit()

    app = create_app(container)
    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        client._vacancy_id = vacancy.id  # type: ignore[attr-defined]
        client._container = container  # type: ignore[attr-defined]
        yield client


@pytest.fixture
async def ui_client() -> AsyncIterator[httpx.AsyncClient]:
    async for client in _client():
        yield client


async def test_vacancies_page_lists_vacancy(ui_client: httpx.AsyncClient) -> None:
    response = await ui_client.get("/ui/vacancies")

    assert response.status_code == 200
    assert "Python-разработчик" in response.text
    assert "Настроить критерии" in response.text


async def test_scoring_page_has_three_steps(ui_client: httpx.AsyncClient) -> None:
    vacancy_id: UUID = ui_client._vacancy_id  # type: ignore[attr-defined]

    response = await ui_client.get(f"/ui/vacancies/{vacancy_id}/scoring")

    assert response.status_code == 200
    body = response.text
    assert "1. Критерии" in body
    assert "2. Вопросы кандидату" in body
    assert "3. Пороги решения" in body
    # Каталог целиком доступен для выбора.
    assert body.count('name="selected_') >= 30


async def test_saving_profile_publishes_shadow(ui_client: httpx.AsyncClient) -> None:
    vacancy_id: UUID = ui_client._vacancy_id  # type: ignore[attr-defined]
    page = await ui_client.get(f"/ui/vacancies/{vacancy_id}/scoring")
    token = ui_client.cookies.get("hirebot_ui_csrf")
    assert page.status_code == 200

    response = await ui_client.post(
        f"/ui/vacancies/{vacancy_id}/scoring",
        data={
            "csrf_token": token,
            "action": "publish",
            "selected_python": "1",
            "weight_python": "5",
            "must_python": "1",
            "question_text_0": "Сколько лет на Python?",
            "question_criterion_0": "python",
            "invite_at": "85",
            "human_at": "55",
            "reject_at": "40",
            "min_coverage": "70",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "shadow" in response.headers["location"]

    after = await ui_client.get(f"/ui/vacancies/{vacancy_id}/scoring")
    assert "v1" in after.text


async def test_saving_without_criteria_shows_message(ui_client: httpx.AsyncClient) -> None:
    vacancy_id: UUID = ui_client._vacancy_id  # type: ignore[attr-defined]
    await ui_client.get(f"/ui/vacancies/{vacancy_id}/scoring")
    token = ui_client.cookies.get("hirebot_ui_csrf")

    response = await ui_client.post(
        f"/ui/vacancies/{vacancy_id}/scoring",
        data={"csrf_token": token, "action": "draft"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "flash" in response.headers["location"]


async def test_create_vacancy_page(ui_client: httpx.AsyncClient) -> None:
    response = await ui_client.get("/ui/vacancies/new")

    assert response.status_code == 200
    assert "Новая вакансия" in response.text
    assert 'name="title"' in response.text


async def test_create_vacancy_redirects_to_criteria(ui_client: httpx.AsyncClient) -> None:
    await ui_client.get("/ui/vacancies/new")
    token = ui_client.cookies.get("hirebot_ui_csrf")

    response = await ui_client.post(
        "/ui/vacancies",
        data={
            "csrf_token": token,
            "title": "Тестовая вакансия",
            "city": "Москва",
            "description": "Описание",
            "channel": "hh",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = response.headers["location"]
    assert "/scoring" in location


async def test_create_vacancy_requires_title(ui_client: httpx.AsyncClient) -> None:
    await ui_client.get("/ui/vacancies/new")
    token = ui_client.cookies.get("hirebot_ui_csrf")

    response = await ui_client.post(
        "/ui/vacancies",
        data={"csrf_token": token, "title": "   "},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "Название" in unquote(response.headers["location"])


async def test_create_vacancy_with_platform_id_links(ui_client: httpx.AsyncClient) -> None:
    """ID площадки связывает вакансию с реальными откликами."""
    await ui_client.get("/ui/vacancies/new")
    token = ui_client.cookies.get("hirebot_ui_csrf")

    response = await ui_client.post(
        "/ui/vacancies",
        data={
            "csrf_token": token,
            "title": "Связанная вакансия",
            "channel": "hh",
            "external_id": "12345678",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "/scoring" in response.headers["location"]


async def test_create_vacancy_with_duplicate_platform_id(ui_client: httpx.AsyncClient) -> None:
    """Повторный ID не создаёт вторую вакансию: отклики не должны раздваиваться."""
    await ui_client.get("/ui/vacancies/new")
    token = ui_client.cookies.get("hirebot_ui_csrf")
    data = {"csrf_token": token, "title": "Дубль", "channel": "hh", "external_id": "dup-777"}

    first = await ui_client.post("/ui/vacancies", data=data, follow_redirects=False)
    second = await ui_client.post("/ui/vacancies", data=data, follow_redirects=False)

    assert first.status_code == 303
    assert second.status_code == 303
    assert "уже есть" in unquote_plus(second.headers["location"])


async def test_delete_vacancy_without_conversations(ui_client: httpx.AsyncClient) -> None:
    """Без диалогов вакансия удаляется полностью."""
    await ui_client.get("/ui/vacancies/new")
    token = ui_client.cookies.get("hirebot_ui_csrf")
    created = await ui_client.post(
        "/ui/vacancies",
        data={"csrf_token": token, "title": "На удаление", "channel": "hh", "external_id": "del-1"},
        follow_redirects=False,
    )
    vacancy_id = created.headers["location"].split("/ui/vacancies/")[1].split("/")[0]

    response = await ui_client.post(
        f"/ui/vacancies/{vacancy_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "удалена" in unquote_plus(response.headers["location"])
    page = await ui_client.get("/ui/vacancies")
    assert "На удаление" not in page.text


async def test_delete_vacancy_with_conversations_hides_it(ui_client: httpx.AsyncClient) -> None:
    """С диалогами вакансия скрывается: историю кандидатов удалять нельзя."""
    from hirebot.domain.entities import Candidate, Conversation, new_id
    from hirebot.domain.enums import Channel, ConversationState

    vacancy_id: UUID = ui_client._vacancy_id  # type: ignore[attr-defined]
    container = ui_client._container  # type: ignore[attr-defined]
    async with container.uow_factory() as uow:
        from hirebot.services.base import DEFAULT_COMPANY_ID

        candidate = await uow.candidates.upsert(
            Candidate(
                id=new_id(),
                channel=Channel.HH,
                external_id="del-cand-1",
                full_name="Кандидат на удаление",
                company_id=DEFAULT_COMPANY_ID,
            )
        )
        await uow.conversations.upsert(
            Conversation(
                id=new_id(),
                channel=Channel.HH,
                external_id="del-conv-1",
                candidate_id=candidate.id,
                vacancy_id=vacancy_id,
                company_id=candidate.company_id,
                state=ConversationState.NEW,
            )
        )
        await uow.commit()

    await ui_client.get("/ui/vacancies")
    token = ui_client.cookies.get("hirebot_ui_csrf")
    response = await ui_client.post(
        f"/ui/vacancies/{vacancy_id}/delete",
        data={"csrf_token": token},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "скрыта" in unquote_plus(response.headers["location"])


def test_must_have_without_selection_is_included(catalog: Any) -> None:
    """«Обязательный» без «выбора» не игнорируется: критерий участвует в оценке."""
    parsed = parse_scoring_form(
        {
            "must_python": "1",
            "weight_python": "5",
            # selected_python намеренно отсутствует
        },
        catalog,
    )

    assert parsed.is_valid
    keys = {item.key for item in parsed.criteria}
    assert keys == {"python"}
    python = next(item for item in parsed.criteria if item.key == "python")
    assert python.must_have is True


def test_must_have_only_still_requires_one_criterion(catalog: Any) -> None:
    """Если вообще ничего не отмечено — ошибка остаётся."""
    parsed = parse_scoring_form({}, catalog)

    assert not parsed.is_valid
    assert any("хотя бы один критерий" in error for error in parsed.errors)


def test_unselected_and_unchecked_criterion_is_ignored(catalog: Any) -> None:
    """Ни «выбор», ни «обязательный» — критерия нет в профиле."""
    parsed = parse_scoring_form({"selected_python": "1"}, catalog)

    keys = {item.key for item in parsed.criteria}
    assert keys == {"python"}
    assert "sql" not in keys
