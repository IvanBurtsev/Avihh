"""Общие данные и утилиты тестов сервисного слоя.

Модуль лежит рядом с тестами (не в пакете ``tests``, чтобы не заводить
``__init__.py`` в общем дереве тестов) и содержит:

* настройки тестового окружения;
* демонстрационные и «пограничные» критерии скоринга;
* помощники наполнения fake-контейнера: вакансия, отклик, ответ кандидата.

Тесты идут только на fake-реализациях (``hirebot.testing.fakes``): без сети,
без БД и без Docker.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

import pytest

from hirebot.config import PolicySettings, Settings
from hirebot.container import Container
from hirebot.domain.entities import Conversation, Vacancy, new_id
from hirebot.domain.enums import Channel, MessageDirection
from hirebot.ports.dto import InboundMessage, InboundResponse, ResumePayload, VacancyPayload
from hirebot.services.base import DEFAULT_COMPANY_ID
from hirebot.testing.fakes import (
    DEMO_CRITERIA,
    DEMO_RESUME_STRONG,
    InMemoryStore,
    fake_channel,
    opened_store,
)

# Явный реэкспорт (PEP 484): тесты скрининга берут слабое резюме из этого модуля,
# поэтому ruff не должен считать импорт неиспользуемым и удалять его.
from hirebot.testing.fakes import DEMO_RESUME_WEAK as DEMO_RESUME_WEAK

VACANCY_EXTERNAL_ID = "hh-vac-test-1"
CONVERSATION_EXTERNAL_ID = "hh-chat-test-1"
CANDIDATE_EXTERNAL_ID = "hh-cand-test-1"
RESUME_EXTERNAL_ID = "hh-resume-test-1"

VACANCY_TITLE = "Python-разработчик"
VACANCY_DESCRIPTION = (
    "Разработка сервисов на Python. Условия: удалённый формат, оформление по ТК РФ. "
    "Задачи: FastAPI, PostgreSQL, REST API."
)

#: Начало виртуального времени: 12:00 по Москве — вне тихих часов.
FIXED_NOW = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)

#: Критерии «пограничного» кандидата: средний балл около порога, провалов нет.
BORDERLINE_CRITERIA: tuple[dict[str, Any], ...] = (
    {
        "key": "python",
        "title": "Опыт разработки на Python",
        "weight": 1.0,
        "must_have": False,
        "expectation": "коммерческий опыт",
    },
    {
        "key": "sql",
        "title": "Работа с PostgreSQL и SQL",
        "weight": 1.0,
        "must_have": False,
        "expectation": "уверенное владение",
    },
    {
        "key": "k8s",
        "title": "Kubernetes в продакшене",
        "weight": 1.0,
        "must_have": False,
        "expectation": "эксплуатация кластеров",
    },
)

#: Критерии для проверки отказа: совпадений в резюме нет вообще.
REJECT_CRITERIA: tuple[dict[str, Any], ...] = (
    {
        "key": "golang",
        "title": "Разработка на Go",
        "weight": 2.0,
        "must_have": False,
        "expectation": "коммерческий опыт",
    },
    {
        "key": "kubernetes",
        "title": "Эксплуатация Kubernetes",
        "weight": 2.0,
        "must_have": False,
        "expectation": "продакшен-опыт",
    },
)

#: Обязательный критерий с малым весом: высокий средний балл не спасает от пробела.
MUST_HAVE_CRITERIA: tuple[dict[str, Any], ...] = (
    {
        "key": "python",
        "title": "Опыт разработки на Python",
        "weight": 10.0,
        "must_have": False,
        "expectation": "коммерческий опыт",
    },
    {
        "key": "kubernetes",
        "title": "Kubernetes в продакшене",
        "weight": 1.0,
        "must_have": True,
        "expectation": "эксплуатация кластеров",
    },
)


def make_settings(**policy_overrides: Any) -> Settings:
    """Настройки для теста: окружение ``test``, без чтения ``.env``."""
    policy = PolicySettings(**policy_overrides) if policy_overrides else PolicySettings()
    return Settings(env="test", _env_file=None, policy=policy)


async def store_of(container: Container) -> InMemoryStore:
    """Хранилище fake-контейнера (только чтение: транзакция откатится)."""
    async with opened_store(container) as store:
        return store


async def seed_vacancy(
    container: Container,
    *,
    criteria: tuple[dict[str, Any], ...] = DEMO_CRITERIA,
    description: str = VACANCY_DESCRIPTION,
) -> Vacancy:
    """Положить вакансию с критериями в БД и в канал (как это делает рекрутер)."""
    vacancy = Vacancy(
        id=new_id(),
        channel=Channel.HH,
        external_id=VACANCY_EXTERNAL_ID,
        title=VACANCY_TITLE,
        company_id=DEFAULT_COMPANY_ID,
        description=description,
        criteria=[dict(criterion) for criterion in criteria],
    )
    async with container.uow_factory() as uow:
        saved = await uow.vacancies.upsert(vacancy)
        await uow.commit()
    fake_channel(container).add_vacancy(
        VacancyPayload(
            external_id=VACANCY_EXTERNAL_ID,
            title=VACANCY_TITLE,
            description=description,
        )
    )
    return saved


async def seed_response(
    container: Container,
    *,
    resume_text: str = DEMO_RESUME_STRONG,
    deny_resume: bool = False,
    conversation_external_id: str = CONVERSATION_EXTERNAL_ID,
    candidate_external_id: str = CANDIDATE_EXTERNAL_ID,
    resume_external_id: str = RESUME_EXTERNAL_ID,
    candidate_name: str = "Тестовый Кандидат",
    received_at: datetime = FIXED_NOW,
) -> InboundResponse:
    """Отклик кандидата вместе с резюме в fake-канале."""
    response = InboundResponse(
        channel=Channel.HH,
        conversation_external_id=conversation_external_id,
        candidate_external_id=candidate_external_id,
        vacancy_external_id=VACANCY_EXTERNAL_ID,
        candidate_name=candidate_name,
        resume_external_id=resume_external_id,
        received_at=received_at,
    )
    channel = fake_channel(container)
    channel.add_response(response)
    channel.add_resume(
        ResumePayload(
            external_id=resume_external_id,
            text=resume_text,
            candidate_name=candidate_name,
            parsed={"timezone": "Europe/Moscow"},
        )
    )
    if deny_resume:
        channel.deny_resume(resume_external_id)
    return response


async def seed_pipeline(
    container: Container,
    *,
    resume_text: str = DEMO_RESUME_STRONG,
    criteria: tuple[dict[str, Any], ...] = DEMO_CRITERIA,
    deny_resume: bool = False,
) -> InboundResponse:
    """Вакансия + отклик: минимальный набор для сквозного сценария."""
    await seed_vacancy(container, criteria=criteria)
    return await seed_response(container, resume_text=resume_text, deny_resume=deny_resume)


async def conversation_by_external(
    container: Container,
    external_id: str = CONVERSATION_EXTERNAL_ID,
) -> Conversation:
    """Найти диалог по внешнему идентификатору."""
    store = await store_of(container)
    for conversation in store.conversations.values():
        if conversation.external_id == external_id:
            return conversation
    msg = f"Диалог {external_id} не найден в fake-хранилище"
    raise AssertionError(msg)


async def conversation_state(container: Container, conversation_id: UUID) -> str:
    """Текущее состояние диалога строкой (удобно для ассертов)."""
    store = await store_of(container)
    return store.conversations[conversation_id].state.value


async def conversation_auto_messages(container: Container, conversation_id: UUID) -> int:
    store = await store_of(container)
    return store.conversations[conversation_id].auto_messages_sent


def add_inbound(
    container: Container,
    text: str,
    *,
    external_id: str,
    at: datetime,
    conversation_external_id: str = CONVERSATION_EXTERNAL_ID,
) -> None:
    """Добавить ответ кандидата в чат площадки."""
    fake_channel(container).add_inbound_message(
        conversation_external_id,
        InboundMessage(
            external_id=external_id,
            direction=MessageDirection.INBOUND,
            text=text,
            created_at=at,
        ),
    )


def set_clock(container: Container, moment: datetime) -> None:
    """Установить виртуальное время контейнера (если часы это умеют)."""
    setter = getattr(container.clock, "set", None)
    if not callable(setter):
        pytest.skip("Часы контейнера не поддерживают установку времени")
    setter(moment)


def next_inbound_time(offset_minutes: int = 30) -> datetime:
    """Момент ответа кандидата — позже времени отклика."""
    return FIXED_NOW + timedelta(minutes=offset_minutes)
