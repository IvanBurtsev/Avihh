"""Тесты наполнения демо-данными.

``seed`` нужен, чтобы показать интерфейс без доступа к площадкам, поэтому
проверяется, что он создаёт согласованный набор: компанию (иначе внешние ключи
не дадут вставить вакансию), вакансию с критериями, кандидатов с резюме,
диалоги с сообщениями — и что скрининг считает настоящий сервис.

Тесты идут на контейнере в памяти: БД, сеть и Docker не нужны.
"""

from __future__ import annotations

from typing import Any

import pytest

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.enums import ConversationState
from hirebot.seed import DEMO_COMPANY_ID, demo_cases, seed_demo_data


@pytest.fixture
def container() -> Any:
    # Пустой контейнер: демо-данные должен создать сам seed, а не фикстура.
    settings = Settings(env="test", log_level="WARNING", log_json=False, _env_file=None)
    return build_fake_container(settings, seed_demo=False)


async def test_seed_creates_company_vacancy_and_dialogs(container: Any) -> None:
    created = await seed_demo_data(container)

    assert created == len(demo_cases())
    async with container.uow_factory() as uow:
        company = await uow.companies.get(DEMO_COMPANY_ID)
        vacancies = await uow.vacancies.list_active(DEMO_COMPANY_ID)
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=50)

    assert company is not None, "компания обязательна: на неё ссылаются вакансии и кандидаты"
    assert len(vacancies) == 1
    assert vacancies[0].criteria, "у вакансии должны быть критерии — иначе балл не считается"
    assert len(conversations) == created


async def test_seed_runs_real_screening(container: Any) -> None:
    """Балл и вердикт в демо-данных считает настоящий ``ScreeningService``."""
    await seed_demo_data(container)

    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=50)
        scored = []
        for conversation in conversations:
            result = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
            if result is not None:
                scored.append(result)

    assert scored, "скрининг должен быть выполнен"
    # Сильный кандидат проходит порог приглашения, слабый — нет.
    assert max(result.total_score for result in scored) >= 70
    assert min(result.total_score for result in scored) < 70
    assert all(result.model for result in scored), "модель обязана попадать в аудит"


async def test_seed_is_idempotent_without_reset(container: Any) -> None:
    first = await seed_demo_data(container)
    second = await seed_demo_data(container)

    assert first > 0
    assert second == 0, "повторный запуск без --reset не должен плодить дубли"


async def test_seed_skips_closed_resume(container: Any) -> None:
    """Кандидат с недоступным резюме остаётся без скрининга — это штатная деградация."""
    await seed_demo_data(container)

    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=50)
        by_name = {}
        for conversation in conversations:
            candidate = await uow.candidates.get(conversation.candidate_id)
            screening = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
            if candidate is not None:
                by_name[candidate.full_name] = screening

    assert by_name["Иван Кузнецов"] is None
    assert by_name["Алексей Петров"] is not None
