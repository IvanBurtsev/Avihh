"""Фикстуры интеграционных тестов: реальные репозитории на SQLite в памяти.

Тесты этой папки **не требуют Docker и Redis**: используется
``sqlite+aiosqlite:///:memory:`` (со ``StaticPool``, иначе каждая сессия
получала бы свою пустую базу) и настоящий ``FieldCipher``. Схема создаётся
``init_models`` — тем же кодом, что и в демо-режиме.

Маркер ``integration`` обязателен: тесты ходят в БД, и их нужно уметь
отделить от чистых unit-тестов (например, при быстром прогоне на коммит).

Про фабрику сущностей
---------------------
Схема БД честно объявляет внешние ключи, поэтому «родителей» нужно реально
сохранять до «детей». Методы :class:`EntityFactory`, создающие сущности, на
которые ссылаются другие таблицы (``vacancy``, ``candidate``, ``conversation``,
``resume``, ``screening``, ``booking``), объявлены **асинхронными и сразу
сохраняют результат** в отдельной короткой транзакции. Сущности-«листья»
(``message``, ``outbox``) остаются синхронными: тесты добавляют их внутри
проверяемой транзакции, и преждевременное сохранение сломало бы проверки
атомарности в ``test_uow.py``.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID, uuid4

import pytest
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker

from hirebot.config import Settings
from hirebot.domain.entities import (
    Booking,
    Candidate,
    Conversation,
    Message,
    OutboxEntry,
    Resume,
    ScreeningCriterion,
    ScreeningResult,
    Vacancy,
)
from hirebot.domain.enums import (
    BookingStatus,
    Channel,
    ConversationState,
    MessageDirection,
    MessageKind,
    ScreeningVerdict,
)
from hirebot.infra.db.models import CompanyRow
from hirebot.infra.db.session import (
    create_engine,
    create_sessionmaker,
    create_uow_factory,
    init_models,
)
from hirebot.infra.security.crypto import FieldCipher
from hirebot.ports.repositories import UnitOfWork

#: Фиксированное «сейчас» для тестов: время не должно влиять на результат.
FROZEN_NOW = datetime(2026, 3, 2, 9, 0, tzinfo=UTC)

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


def _build_settings(**overrides: Any) -> Settings:
    base: dict[str, Any] = {
        "env": "test",
        "database": {"dsn": "sqlite+aiosqlite:///:memory:"},
        "security": {"pii_key": FieldCipher.generate_key()},
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class EntityFactory:
    """Фабрика доменных сущностей, которая сохраняет предпосылки.

    Позволяет в тесте указывать только то, что важно для конкретной проверки:
    остальные поля заполнены так, чтобы сущность была валидной и уже
    существовала в БД вместе со своими «родителями».
    """

    def __init__(self, uow_factory: UowFactory, company_id: UUID) -> None:
        self._uow_factory = uow_factory
        self.company_id = company_id
        self.now = FROZEN_NOW
        self._sequence = 0

    def _next_external_id(self, prefix: str) -> str:
        """Уникальный внешний идентификатор.

        Значения по умолчанию не должны повторяться: иначе второй вызов
        ``make.candidate()`` в одном тесте упрётся в уникальный индекс
        ``(channel, external_id)``.
        """
        self._sequence += 1
        return f"{prefix}-{self._sequence}"

    # --- асинхронные: создают и сразу сохраняют -------------------------------

    async def vacancy(self, **overrides: Any) -> Vacancy:
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "channel": Channel.HH,
            "external_id": self._next_external_id("vacancy"),
            "title": "Менеджер по продажам",
            "company_id": self.company_id,
            "description": "Активные продажи B2B",
            "city": "Москва",
            "criteria": [{"key": "sales", "title": "Опыт продаж", "weight": 2.0}],
            "created_at": self.now,
            "updated_at": self.now,
        }
        defaults.update(overrides)
        entity = Vacancy(**defaults)
        async with self._uow_factory() as uow:
            await uow.vacancies.upsert(entity)
        return entity

    async def candidate(self, **overrides: Any) -> Candidate:
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "channel": Channel.HH,
            "external_id": self._next_external_id("candidate"),
            "full_name": "Иван Петров",
            "company_id": self.company_id,
            "phone": "+7 999 123-45-67",
            "email": "ivan@example.com",
            "city": "Москва",
            "created_at": self.now,
        }
        defaults.update(overrides)
        entity = Candidate(**defaults)
        async with self._uow_factory() as uow:
            await uow.candidates.upsert(entity)
        return entity

    async def conversation(self, **overrides: Any) -> Conversation:
        candidate_id = overrides.pop("candidate_id", None)
        vacancy_id = overrides.pop("vacancy_id", None)
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "company_id": self.company_id,
            "candidate_id": candidate_id or (await self.candidate()).id,
            "vacancy_id": vacancy_id or (await self.vacancy()).id,
            "channel": Channel.HH,
            "external_id": self._next_external_id("conversation"),
            "state": ConversationState.NEW,
            "created_at": self.now,
            "updated_at": self.now,
        }
        defaults.update(overrides)
        entity = Conversation(**defaults)
        async with self._uow_factory() as uow:
            await uow.conversations.upsert(entity)
        return entity

    async def resume(self, **overrides: Any) -> Resume:
        candidate_id = overrides.pop("candidate_id", None)
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "candidate_id": candidate_id or (await self.candidate()).id,
            "channel": Channel.HH,
            "external_id": self._next_external_id("resume"),
            "raw_text": "Иван Петров. Опыт продаж 5 лет. Телефон +7 999 123-45-67",
            "parsed": {"total_experience_months": 60},
            "source_url": "https://hh.ru/resume/1",
            "fetched_at": self.now,
        }
        defaults.update(overrides)
        entity = Resume(**defaults)
        async with self._uow_factory() as uow:
            await uow.resumes.save(entity)
        return entity

    async def screening(self, **overrides: Any) -> ScreeningResult:
        resume_id = overrides.pop("resume_id", None)
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "resume_id": resume_id or (await self.resume()).id,
            "vacancy_id": overrides.pop("vacancy_id", None) or (await self.vacancy()).id,
            "candidate_id": overrides.pop("candidate_id", None) or (await self.candidate()).id,
            "total_score": 85.0,
            "verdict": ScreeningVerdict.MATCH,
            "criteria": [
                ScreeningCriterion(
                    key="sales",
                    title="Опыт продаж",
                    weight=2.0,
                    score=90.0,
                    must_have=True,
                    evidence="5 лет в продажах",
                )
            ],
            "summary": "Подходит по опыту",
            "gaps": [],
            "strengths": ["опыт B2B"],
            "questions": ["Какая средняя сделка?"],
            "model": "test-model",
            "prompt_version": "v1",
            "created_at": self.now,
        }
        defaults.update(overrides)
        entity = ScreeningResult(**defaults)
        async with self._uow_factory() as uow:
            await uow.screenings.save(entity)
        return entity

    async def booking(self, **overrides: Any) -> Booking:
        start = overrides.pop("start", self.now + timedelta(days=1))
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "conversation_id": overrides.pop("conversation_id", None) or (await self.conversation()).id,
            "candidate_id": overrides.pop("candidate_id", None) or (await self.candidate()).id,
            "vacancy_id": overrides.pop("vacancy_id", None) or (await self.vacancy()).id,
            "manager_id": "manager-1",
            "start": start,
            "end": start + timedelta(hours=1),
            "timezone": "Europe/Moscow",
            "status": BookingStatus.PROPOSED,
            "created_at": self.now,
            "updated_at": self.now,
        }
        defaults.update(overrides)
        entity = Booking(**defaults)
        async with self._uow_factory() as uow:
            await uow.bookings.save(entity)
        return entity

    # --- синхронные: сохраняются самим тестом внутри его транзакции -----------

    def message(self, **overrides: Any) -> Message:
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "conversation_id": uuid4(),
            "direction": MessageDirection.OUTBOUND,
            "body": "Здравствуйте! Уточните, пожалуйста, опыт продаж.",
            "kind": MessageKind.CLARIFYING_QUESTION,
            "created_at": self.now,
        }
        defaults.update(overrides)
        return Message(**defaults)

    def outbox(self, **overrides: Any) -> OutboxEntry:
        defaults: dict[str, Any] = {
            "id": uuid4(),
            "topic": "process_conversation",
            "payload": {"conversation_id": str(uuid4())},
            "dedupe_key": f"conversation:{uuid4().hex}:step:screen",
            "created_at": self.now,
            "available_at": self.now,
        }
        defaults.update(overrides)
        return OutboxEntry(**defaults)


@pytest.fixture
def settings() -> Settings:
    return _build_settings()


@pytest.fixture
def settings_factory() -> Callable[..., Settings]:
    return _build_settings


@pytest.fixture
async def engine(settings: Settings) -> AsyncIterator[AsyncEngine]:
    """Движок на SQLite в памяти со схемой, созданной по моделям."""
    engine = create_engine(settings)
    await init_models(engine)
    try:
        yield engine
    finally:
        await engine.dispose()


@pytest.fixture
def sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    return create_sessionmaker(engine)


@pytest.fixture
def cipher(settings: Settings) -> FieldCipher:
    return FieldCipher.from_settings(settings)


@pytest.fixture
def uow_factory(
    settings: Settings,
    sessionmaker: async_sessionmaker[AsyncSession],
    cipher: FieldCipher,
) -> UowFactory:
    """Фабрика UoW, работающая на общем для теста движке."""
    return create_uow_factory(settings, sessionmaker=sessionmaker, cipher=cipher)


@pytest.fixture
async def company_id(sessionmaker: async_sessionmaker[AsyncSession]) -> UUID:
    """Создать компанию: у большинства таблиц есть FK на неё."""
    company = uuid4()
    async with sessionmaker() as session:
        session.add(
            CompanyRow(
                id=company,
                name="ООО Тестовая компания",
                slug=f"test-{company.hex[:8]}",
                created_at=FROZEN_NOW,
            )
        )
        await session.commit()
    return company


@pytest.fixture
def make(uow_factory: UowFactory, company_id: UUID) -> EntityFactory:
    return EntityFactory(uow_factory, company_id)
