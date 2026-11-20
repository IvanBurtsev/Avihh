"""Тесты сроков хранения и прав субъекта ПДн (152-ФЗ).

Проверяется то, что оператор обязан уметь подтвердить проверяющему:

* данные, срок хранения которых истёк, **удаляются** (в том числе текст резюме);
* свежие данные **не трогаются**;
* субъект может получить **все** свои данные одним вызовом;
* удаление по требованию действительно удаляет ПДн, но **не ломает воронку**:
  диалог остаётся, а имя и контакты — нет.

Тесты на контейнере в памяти: без сети, БД и Docker.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from hirebot.config import PrivacySettings, Settings
from hirebot.container import build_fake_container
from hirebot.domain.enums import Channel, ConversationState
from hirebot.services import IngestService
from hirebot.services.privacy import ANONYMIZED_NAME, PrivacyService


def make_settings(*, resume_days: int = 180) -> Settings:
    return Settings(
        env="test",
        log_level="WARNING",
        log_json=False,
        _env_file=None,
        privacy=PrivacySettings(resume_retention_days=resume_days),
    )


@pytest.fixture
async def container() -> Any:
    result = build_fake_container(make_settings())
    await IngestService(result).poll(Channel.HH)
    return result


async def _first_conversation(container: Any) -> Any:
    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=10)
    assert conversations
    return conversations[0]


async def test_purge_removes_expired_and_keeps_fresh(container: Any) -> None:
    """Истёкшие данные удаляются, свежие остаются — иначе потеряется работа."""
    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=10)
    assert len(conversations) >= 2, "нужны два кандидата: один истекает, другой остаётся"

    expired_conversation = conversations[0]
    fresh_conversation = conversations[1]
    fresh = container.clock.now()

    async with container.uow_factory() as uow:
        old_resume = await uow.resumes.get_latest_for_candidate(expired_conversation.candidate_id)
        assert old_resume is not None
        # Сдвигаем одно резюме в прошлое: срок хранения истёк.
        old_resume.fetched_at = fresh - timedelta(days=400)
        await uow.resumes.save(old_resume)

        untouched = await uow.resumes.get_latest_for_candidate(fresh_conversation.candidate_id)
        assert untouched is not None
        untouched_id = untouched.id
        await uow.commit()

    report = await PrivacyService(container).purge_expired()

    assert report.stats.resumes >= 1, "просроченное резюме должно быть удалено"

    async with container.uow_factory() as uow:
        removed = await uow.resumes.get_latest_for_candidate(expired_conversation.candidate_id)
        remaining = await uow.resumes.get_latest_for_candidate(fresh_conversation.candidate_id)

    assert removed is None, "просроченное резюме обязано исчезнуть"
    assert remaining is not None, "свежее резюме другого кандидата не должно пострадать"
    assert remaining.id == untouched_id


async def test_purge_is_idempotent(container: Any) -> None:
    """Второй прогон ничего не удаляет: задача рассчитана на расписание."""
    await PrivacyService(container).purge_expired()
    second = await PrivacyService(container).purge_expired()

    assert second.total == 0


async def test_export_contains_all_candidate_data(container: Any) -> None:
    """Субъект получает всё: резюме, переписку, скрининг, брони."""
    conversation = await _first_conversation(container)
    payload = await PrivacyService(container).export_candidate(conversation.candidate_id)

    assert payload is not None
    assert payload["candidate"]["id"] == str(conversation.candidate_id)
    assert payload["candidate"]["full_name"]
    assert "resumes" in payload
    assert "conversations" in payload
    assert "messages" in payload
    assert "screenings" in payload


async def test_export_of_unknown_candidate_is_none(container: Any) -> None:
    assert await PrivacyService(container).export_candidate(uuid4()) is None


async def test_erase_removes_personal_data_but_keeps_funnel(container: Any) -> None:
    """Удаление по требованию: ПДн нет, история воронки сохранена."""
    conversation = await _first_conversation(container)
    candidate_id = conversation.candidate_id

    erased = await PrivacyService(container).erase_candidate(candidate_id)
    assert erased is True

    async with container.uow_factory() as uow:
        candidate = await uow.candidates.get(candidate_id)
        resume = await uow.resumes.get_latest_for_candidate(candidate_id)
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=50)
        messages = await uow.messages.list_for_conversation(conversation.id, limit=50)
        screenings = await uow.screenings.get_latest(candidate_id, conversation.vacancy_id)

    assert candidate is not None, "учётная запись остаётся обезличенной"
    assert candidate.full_name == ANONYMIZED_NAME
    assert candidate.phone is None
    assert candidate.email is None

    assert resume is None, "текст резюме обязан быть удалён"
    assert screenings is None
    assert messages == []
    assert any(item.id == conversation.id for item in conversations), (
        "диалог остаётся: без него теряется история работы рекрутера"
    )


async def test_erase_unknown_candidate_returns_false(container: Any) -> None:
    assert await PrivacyService(container).erase_candidate(uuid4()) is False


async def test_export_after_erase_shows_no_personal_data(container: Any) -> None:
    """После удаления повторная выгрузка не должна содержать прежних ПДн."""
    conversation = await _first_conversation(container)
    service = PrivacyService(container)

    before = await service.export_candidate(conversation.candidate_id)
    assert before is not None
    original_name = before["candidate"]["full_name"]

    await service.erase_candidate(conversation.candidate_id)
    after = await service.export_candidate(conversation.candidate_id)

    assert after is not None
    assert after["candidate"]["full_name"] == ANONYMIZED_NAME
    assert after["candidate"]["full_name"] != original_name
    assert after["resumes"] == []
    assert after["messages"] == []
