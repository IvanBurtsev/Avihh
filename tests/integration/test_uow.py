"""Интеграционные тесты Unit of Work.

Главное, что здесь проверяется: транзакционная граница действительно работает.
Сценарий «сообщение + смена состояния диалога + задача в outbox» обязан быть
атомарным — иначе воркер увидит половину изменений и напишет кандидату не то.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hirebot.domain.enums import BookingStatus, ConversationState, MessageDirection, OutboxStatus
from hirebot.infra.db.models import MessageRow, OutboxRow
from hirebot.ports.repositories import UnitOfWork

from .conftest import FROZEN_NOW, EntityFactory

pytestmark = pytest.mark.integration

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


async def _count(sessionmaker: async_sessionmaker[AsyncSession], model: type[Any]) -> int:
    """Число строк в таблице — без ручного SQL и без подстановки имён в строку."""
    async with sessionmaker() as session:
        value = (await session.execute(select(func.count()).select_from(model))).scalar()
    return int(value or 0)


async def test_clean_exit_commits_implicitly(
    uow_factory: UowFactory, make: EntityFactory, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Забытый ``commit()`` не должен приводить к молчаливой потере работы."""
    async with uow_factory() as uow:
        await uow.outbox.add(make.outbox(dedupe_key="implicit-commit"))

    assert await _count(sessionmaker, OutboxRow) == 1


async def test_explicit_commit_persists(
    uow_factory: UowFactory, make: EntityFactory, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with uow_factory() as uow:
        await uow.outbox.add(make.outbox(dedupe_key="explicit-commit"))
        await uow.commit()

    assert await _count(sessionmaker, OutboxRow) == 1


async def test_exception_rolls_back_everything(
    uow_factory: UowFactory, make: EntityFactory, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    conversation = await make.conversation(state=ConversationState.NEW)
    async with uow_factory() as uow:
        await uow.conversations.upsert(conversation)

    # Несколько операторов в блоке — суть проверки: состояние, сообщение и задача
    # должны исчезнуть вместе после падения в середине сценария.
    with pytest.raises(RuntimeError, match="падение"):  # noqa: PT012
        async with uow_factory() as uow:
            conversation.state = ConversationState.INVITED
            await uow.conversations.upsert(conversation)
            await uow.messages.add(make.message(conversation_id=conversation.id))
            await uow.outbox.add(make.outbox(dedupe_key="rolled-back"))
            msg = "падение в середине сценария"
            raise RuntimeError(msg)

    assert await _count(sessionmaker, MessageRow) == 0
    assert await _count(sessionmaker, OutboxRow) == 0
    async with uow_factory() as uow:
        stored = await uow.conversations.get(conversation.id)
    assert stored is not None
    assert stored.state is ConversationState.NEW


async def test_explicit_rollback_discards_changes(
    uow_factory: UowFactory, make: EntityFactory, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    async with uow_factory() as uow:
        await uow.outbox.add(make.outbox(dedupe_key="manual-rollback"))
        await uow.rollback()

    assert await _count(sessionmaker, OutboxRow) == 0


async def test_atomic_funnel_step_is_all_or_nothing(uow_factory: UowFactory, make: EntityFactory) -> None:
    """Успешный сценарий: состояние, сообщение и задача появляются вместе."""
    conversation = await make.conversation(state=ConversationState.AWAITING_ANSWER)
    async with uow_factory() as uow:
        await uow.conversations.upsert(conversation)

    async with uow_factory() as uow:
        conversation.state = ConversationState.INVITED
        await uow.conversations.upsert(conversation)
        await uow.messages.add(
            make.message(
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                body="Приглашаем на собеседование",
            )
        )
        await uow.outbox.add(make.outbox(dedupe_key="invitation-sent"))
        await uow.commit()

    async with uow_factory() as uow:
        stored = await uow.conversations.get(conversation.id)
        messages = await uow.messages.list_for_conversation(conversation.id)
        claimed = await uow.outbox.claim_batch(limit=10, now=FROZEN_NOW)

    assert stored is not None
    assert stored.state is ConversationState.INVITED
    assert len(messages) == 1
    assert messages[0].body == "Приглашаем на собеседование"
    assert [entry.status for entry in claimed] == [OutboxStatus.PENDING]


async def test_idempotency_guard_survives_transaction(uow_factory: UowFactory, make: EntityFactory) -> None:
    """Захват ключа и бронь коммитятся вместе: повтор не создаст вторую встречу."""
    booking = await make.booking(idempotency_key="funnel-key-1")

    async with uow_factory() as uow:
        acquired = await uow.idempotency.acquire("funnel-key-1", scope="book_meeting")
        assert acquired is True
        await uow.bookings.save(booking)
        await uow.commit()

    async with uow_factory() as uow:
        again = await uow.idempotency.acquire("funnel-key-1", scope="book_meeting")
        existing = await uow.bookings.get_by_idempotency_key("funnel-key-1")

    assert again is False
    assert existing is not None
    assert existing.id == booking.id
    assert existing.status is BookingStatus.PROPOSED


async def test_session_is_released_after_exit(uow_factory: UowFactory) -> None:
    uow = uow_factory()
    async with uow:
        assert uow.session is not None

    with pytest.raises(RuntimeError, match="не открыт"):
        _ = uow.session


async def test_uow_can_be_reentered(uow_factory: UowFactory, make: EntityFactory) -> None:
    """После выхода UoW можно открыть снова — он получает новую сессию."""
    uow = uow_factory()
    async with uow:
        first_session = uow.session
    async with uow:
        assert uow.session is not first_session
        await uow.outbox.add(make.outbox(dedupe_key="reentered"))


async def test_repositories_are_bound_to_the_same_session(uow_factory: UowFactory) -> None:
    async with uow_factory() as uow:
        session = uow.session
        for repository in (
            uow.vacancies,
            uow.candidates,
            uow.resumes,
            uow.conversations,
            uow.messages,
            uow.screenings,
            uow.bookings,
            uow.outbox,
            uow.idempotency,
            uow.audit,
        ):
            # Все репозитории работают в одной транзакции — иначе атомарность
            # сценария воронки была бы невозможна.
            assert repository._session is session
