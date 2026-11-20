"""Тесты очереди подтверждения.

Главное, что здесь проверяется — **кандидат не получает сообщение без решения
человека**. Это режим, в котором бота безопасно включать на реальных людях,
поэтому тесты фиксируют:

* при включённом подтверждении отправки нет, а черновик появляется;
* черновик содержит целевое состояние и объяснение для рекрутера;
* одобрение отправляет сообщение **один раз** (повтор не дублирует);
* отредактированный текст уходит вместо исходного;
* отклонение не отправляет ничего и оставляет диалог человеку;
* новый черновик вытесняет старый — одобрить устаревшее сообщение нельзя.

Всё на fake-адаптерах: сети, БД и Docker не нужно.
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from hirebot.config import PolicySettings, Settings
from hirebot.container import build_fake_container
from hirebot.domain.enums import Channel, ConversationState, DraftStatus, MessageKind
from hirebot.domain.errors import NotFound, PolicyViolation
from hirebot.services import ConversationService, IngestService
from hirebot.services.approval import ApprovalService
from hirebot.testing.fakes import fake_channel


def make_settings(*, approval: bool = True) -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        log_json=False,
        _env_file=None,
        policy=PolicySettings(require_human_approval=approval),
    )


@pytest.fixture
async def container() -> Any:
    """Контейнер в режиме обязательного подтверждения, с одним свежим откликом."""
    result = build_fake_container(make_settings(approval=True))
    await IngestService(result).poll(Channel.HH)
    return result


async def _conversations(container: Any) -> list[Any]:
    async with container.uow_factory() as uow:
        return list(await uow.conversations.list_by_state(list(ConversationState), limit=50))


async def _first_conversation(container: Any) -> Any:
    found = await _conversations(container)
    assert found, "нужен хотя бы один диалог"
    return found[0]


async def test_approval_mode_does_not_send_but_prepares_draft(container: Any) -> None:
    """Ключевая гарантия: без одобрения рекрутера кандидат ничего не получает."""
    conversation = await _first_conversation(container)
    chat = fake_channel(container, Channel.HH)

    outcome = await ConversationService(container).process(conversation.id)

    assert chat.texts_for(conversation.external_id) == [], "сообщение не должно уйти кандидату"
    assert outcome.state is ConversationState.HUMAN_REVIEW
    assert outcome.action_taken.endswith("_draft")

    async with container.uow_factory() as uow:
        draft = await uow.drafts.get_pending_for_conversation(conversation.id)

    assert draft is not None, "черновик обязан появиться: иначе режим апрува бесполезен"
    assert draft.body.strip()
    assert draft.status is DraftStatus.PENDING
    # Целевое состояние сохранено: без него нельзя завершить шаг после одобрения.
    assert draft.target_state is not ConversationState.HUMAN_REVIEW


async def test_approved_draft_is_sent_exactly_once(container: Any) -> None:
    conversation = await _first_conversation(container)
    await ConversationService(container).process(conversation.id)
    async with container.uow_factory() as uow:
        draft = await uow.drafts.get_pending_for_conversation(conversation.id)
    assert draft is not None

    service = ApprovalService(container)
    message = await service.approve(draft.id, reviewer="recruiter")

    assert message.body == draft.body

    chat = fake_channel(container, Channel.HH)
    texts = chat.texts_for(conversation.external_id)
    assert len(texts) == 1, "сообщение должно уйти ровно один раз"

    async with container.uow_factory() as uow:
        stored = await uow.drafts.get(draft.id)
        updated = await uow.conversations.get(conversation.id)

    assert stored is not None
    assert stored.status is DraftStatus.APPROVED
    assert stored.reviewer == "recruiter"
    assert updated is not None
    assert updated.state is conversation.state or updated.state is draft.target_state


async def test_repeated_approval_is_rejected_not_duplicated(container: Any) -> None:
    """Повторное одобрение — ошибка, а не второе сообщение кандидату."""
    conversation = await _first_conversation(container)
    await ConversationService(container).process(conversation.id)
    async with container.uow_factory() as uow:
        draft = await uow.drafts.get_pending_for_conversation(conversation.id)
    assert draft is not None

    service = ApprovalService(container)
    await service.approve(draft.id, reviewer="recruiter")

    with pytest.raises(PolicyViolation):
        await service.approve(draft.id, reviewer="recruiter")

    chat = fake_channel(container, Channel.HH)
    assert len(chat.texts_for(conversation.external_id)) == 1


async def test_edited_text_is_sent_instead_of_draft(container: Any) -> None:
    """Рекрутер правит формулировку — уходит его версия, ответственность на нём."""
    conversation = await _first_conversation(container)
    await ConversationService(container).process(conversation.id)
    async with container.uow_factory() as uow:
        draft = await uow.drafts.get_pending_for_conversation(conversation.id)
    assert draft is not None

    edited = "Здравствуйте! Меня зовут Анна, я рекрутер компании. Удобно созвониться завтра?"
    message = await ApprovalService(container).approve(draft.id, reviewer="recruiter", text=edited)

    assert message.body == edited
    chat = fake_channel(container, Channel.HH)
    assert chat.texts_for(conversation.external_id) == [edited]


async def test_rejected_draft_is_not_sent(container: Any) -> None:
    conversation = await _first_conversation(container)
    await ConversationService(container).process(conversation.id)
    async with container.uow_factory() as uow:
        draft = await uow.drafts.get_pending_for_conversation(conversation.id)
    assert draft is not None

    service = ApprovalService(container)
    await service.reject(draft.id, reviewer="recruiter", reason="не подходит по опыту")

    chat = fake_channel(container, Channel.HH)
    assert chat.texts_for(conversation.external_id) == []

    async with container.uow_factory() as uow:
        stored = await uow.drafts.get(draft.id)
        updated = await uow.conversations.get(conversation.id)

    assert stored is not None
    assert stored.status is DraftStatus.REJECTED
    assert stored.review_comment == "не подходит по опыту"
    # Диалог остаётся человеку: отклонение черновика — не отказ кандидату.
    assert updated is not None
    assert updated.state is ConversationState.HUMAN_REVIEW


async def test_new_draft_supersedes_previous(container: Any) -> None:
    """Устаревший черновик нельзя одобрить: решение бота уже неактуально."""
    conversation = await _first_conversation(container)
    service = ApprovalService(container)

    first = await service.prepare(
        conversation.id,
        body="Первый вариант",
        kind=MessageKind.CLARIFYING_QUESTION,
        target_state=ConversationState.QUESTIONS_SENT,
    )
    second = await service.prepare(
        conversation.id,
        body="Второй вариант",
        kind=MessageKind.INVITATION,
        target_state=ConversationState.INVITED,
    )

    async with container.uow_factory() as uow:
        reloaded = await uow.drafts.get(first.id)

    assert reloaded is not None
    assert reloaded.status is DraftStatus.SUPERSEDED
    assert second.id != first.id

    with pytest.raises(PolicyViolation):
        await service.approve(first.id, reviewer="recruiter")


async def test_approving_unknown_draft_raises_not_found(container: Any) -> None:
    with pytest.raises(NotFound):
        await ApprovalService(container).approve(uuid4(), reviewer="recruiter")


async def test_list_pending_has_context_for_recruiter(container: Any) -> None:
    """Очередь показывает, кто перед нами и почему бот предлагает этот текст."""
    conversation = await _first_conversation(container)
    await ConversationService(container).process(conversation.id)

    pending = await ApprovalService(container).list_pending()

    assert len(pending) == 1
    item = pending[0]
    assert item.conversation_id == conversation.id
    assert item.candidate_name
    assert item.body
    assert item.reason, "рекрутеру нужно объяснение, а не просто текст"


async def test_without_approval_mode_message_is_sent_directly() -> None:
    """Обратная проверка: при выключенном подтверждении бот отправляет сам."""
    plain = build_fake_container(make_settings(approval=False))
    await IngestService(plain).poll(Channel.HH)
    conversation = await _first_conversation(plain)

    outcome = await ConversationService(plain).process(conversation.id)

    chat = fake_channel(plain, Channel.HH)
    assert outcome.state is not ConversationState.HUMAN_REVIEW
    assert chat.texts_for(conversation.external_id), "без апрува сообщение уходит сразу"
