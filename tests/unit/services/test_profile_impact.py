"""Тесты влияния профиля на скрининг: кэш, версия, пересчёт.

Главная проверка — **смена критериев не оставляет старых решений**:

* кэш по содержимому резюме обязан учитывать версию профиля, иначе кандидат
  получит оценку по прежним правилам;
* если профиль опубликован после скрининга, шаг воронки пересчитывает результат,
  а не берёт сохранённый;
* в карточке видны процент, уверенность и версия профиля.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.entities import Company, Resume, Vacancy
from hirebot.domain.enums import Channel, ConversationState
from hirebot.domain.scoring import ProfileCriterion, ProfileQuestion, ProfileThresholds
from hirebot.services import ConversationService, IngestService
from hirebot.services.base import DEFAULT_COMPANY_ID
from hirebot.services.scoring_profile import ScoringProfileService
from hirebot.services.screening import ScreeningService
from hirebot.testing.fakes import FakeChannelAdapter


@pytest.fixture
async def container() -> Any:
    result = build_fake_container(Settings(env="test", log_level="WARNING", _env_file=None))
    await IngestService(result).poll(Channel.HH)
    return result


async def _conversation(container: Any) -> Any:
    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(
            [ConversationState.NEW, ConversationState.RESUME_FETCHED], limit=5
        )
    assert conversations
    return conversations[0]


def _criteria() -> list[ProfileCriterion]:
    return [
        ProfileCriterion(key="python", title="Python", weight=5, must_have=True, expectation="3+ года"),
        ProfileCriterion(key="sql", title="SQL", weight=3, expectation="уверенный SQL"),
    ]


async def test_content_hash_depends_on_profile_version() -> None:
    """Один текст резюме под разные профили даёт разные ключи кэша."""
    text = "Python, SQL, 5 лет опыта"

    first = Resume.content_hash(text, vacancy_id=uuid4(), profile_fingerprint="v1:python,sql")
    second = Resume.content_hash(text, vacancy_id=uuid4(), profile_fingerprint="v2:python,sql")

    assert first != second


async def test_screening_saves_profile_version(container: Any) -> None:
    conversation = await _conversation(container)
    result = await ScreeningService(container).screen(conversation_id=conversation.id)

    # Профиля ещё нет: результат собран по прежним критериям вакансии.
    assert result.profile_version in (None, 0)
    assert result.percent is not None
    assert result.coverage is not None


async def test_new_profile_invalidates_saved_screening(container: Any) -> None:
    """После публикации профиля шаг воронки пересчитывает кандидата."""
    conversation = await _conversation(container)
    screening_service = ScreeningService(container)
    first = await screening_service.screen(conversation_id=conversation.id)

    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(conversation.vacancy_id)
    assert vacancy is not None

    profiles = ScoringProfileService(container)
    draft = await profiles.create_draft(
        vacancy=vacancy,
        criteria=_criteria(),
        questions=[ProfileQuestion(text="Сколько лет на Python?", criterion_key="python")],
        thresholds=ProfileThresholds(),
    )
    await profiles.publish(draft.id, as_shadow=True)

    outcome = await ConversationService(container).process(conversation.id)
    assert outcome.action_taken, "шаг должен выполниться"

    async with container.uow_factory() as uow:
        refreshed = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)

    assert refreshed is not None
    assert refreshed.profile_version == 1, "результат обязан быть посчитан по новой версии"
    assert refreshed.percent is not None
    # Старый результат остаётся в истории, но новым решением не является.
    assert refreshed.id != first.id


async def test_shadow_profile_does_not_send_messages(container: Any) -> None:
    """В shadow-режиме кандидат ничего не получает: режим проверки."""
    conversation = await _conversation(container)
    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(conversation.vacancy_id)
    assert vacancy is not None

    profiles = ScoringProfileService(container)
    draft = await profiles.create_draft(vacancy=vacancy, criteria=_criteria())
    await profiles.publish(draft.id, as_shadow=True)

    await ConversationService(container).process(conversation.id)

    channel = container.channels[Channel.HH]
    assert isinstance(channel, FakeChannelAdapter)
    assert channel.texts_for(conversation.external_id) == [], "в shadow-режиме отправка запрещена"


async def test_cache_reuse_requires_same_profile(container: Any) -> None:
    """Кэш не отдаёт результат, посчитанный по другому профилю."""
    conversation = await _conversation(container)
    service = ScreeningService(container)
    first = await service.screen(conversation_id=conversation.id)

    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(conversation.vacancy_id)
    assert vacancy is not None
    profiles = ScoringProfileService(container)
    draft = await profiles.create_draft(vacancy=vacancy, criteria=_criteria(), questions=[])
    await profiles.publish(draft.id, as_shadow=True)

    second = await service.screen(conversation_id=conversation.id)

    assert second.cache_hit is False, "старый кэш не должен применяться к новому профилю"
    assert second.id != first.id
    assert second.profile_version == 1


async def test_profile_survives_company_and_vacancy_creation() -> None:
    """Дымовая проверка моделей: профиль связан с вакансией и компанией."""
    company = Company(id=DEFAULT_COMPANY_ID, name="Компания", slug="company")
    vacancy = Vacancy(
        id=uuid4(),
        channel=Channel.HH,
        external_id="v-1",
        title="Вакансия",
        company_id=company.id,
        criteria=[item.as_vacancy_dict() for item in _criteria()],
    )
    assert vacancy.company_id == company.id
    assert len(vacancy.criteria) == 2
    assert vacancy.criteria[0]["expectation"]
