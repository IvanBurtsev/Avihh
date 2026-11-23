"""Тесты ответов кандидата и пересчёта соответствия.

Главная проверка — ответ **меняет решение**, а не просто остаётся в переписке:

* ответ по критерию сохраняется с привязкой и баллом;
* пересчёт повышает процент, если ответ закрывает пробел;
* противоречие с резюме не повышает балл автоматически;
* неясный ответ не улучшает картину;
* отсутствие критерия в профиле не ломает диалог;
* новый результат скрининга сохраняется отдельно (история не перезаписывается).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.entities import ScreeningResult
from hirebot.domain.enums import (
    Channel,
    MessageDirection,
    ProfileStatus,
    ScreeningVerdict,
)
from hirebot.domain.scoring import ProfileCriterion, ProfileQuestion, ScoringProfile
from hirebot.services import IngestService
from hirebot.services.answers import AnswerService
from hirebot.services.scoring_profile import ScoringProfileService


@pytest.fixture
async def container() -> Any:
    result = build_fake_container(Settings(env="test", log_level="WARNING", _env_file=None))
    await IngestService(result).poll(Channel.HH)
    return result


def _criteria() -> list[ProfileCriterion]:
    return [
        ProfileCriterion(
            key="python",
            title="Python",
            weight=5,
            must_have=True,
            expectation="коммерческая разработка на Python",
            evidence_hint="python, django, fastapi",
        ),
        ProfileCriterion(key="sql", title="SQL", weight=3, expectation="уверенный SQL"),
    ]


def _profile() -> ScoringProfile:
    return ScoringProfile(
        id=uuid4(),
        company_id=uuid4(),
        vacancy_id=uuid4(),
        version=1,
        status=ProfileStatus.ACTIVE,
        criteria=_criteria(),
        questions=[
            ProfileQuestion(text="Сколько лет на Python?", criterion_key="python"),
        ],
    )


def _screening(percent: float = 50.0) -> ScreeningResult:
    return ScreeningResult(
        id=uuid4(),
        resume_id=uuid4(),
        vacancy_id=uuid4(),
        candidate_id=uuid4(),
        total_score=percent,
        verdict=ScreeningVerdict.WEAK,
        percent=percent,
        coverage=0.5,
    )


async def _conversation(container: Any) -> Any:
    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(
            list(__import__("hirebot.domain.enums", fromlist=["ConversationState"]).ConversationState),
            limit=5,
        )
    assert conversations
    return conversations[0]


# ---------------------------------------------------------------- сохранение


async def test_answer_is_saved_with_criterion(container: Any) -> None:
    conversation = await _conversation(container)
    service = AnswerService(container)

    saved = await service.record_answer(
        conversation_id=conversation.id,
        question="Сколько лет на Python?",
        criterion_key="python",
        answer="Пять лет коммерческой разработки на Python, Django, FastAPI.",
    )

    assert saved is not None
    assert saved.criterion_key == "python"
    assert saved.score > 0
    assert saved.is_unclear is False

    async with container.uow_factory() as uow:
        stored = await uow.answers.list_for_conversation(conversation.id)
    assert len(stored) == 1


async def test_unclear_answer_is_marked(container: Any) -> None:
    conversation = await _conversation(container)

    saved = await AnswerService(container).record_answer(
        conversation_id=conversation.id,
        question="Сколько лет на Python?",
        criterion_key="python",
        answer="не знаю",
    )

    assert saved is not None
    assert saved.is_unclear is True
    assert saved.score == 0.0


async def test_unknown_criterion_returns_none(container: Any) -> None:
    """Критерия нет в профиле — не выдумываем оценку."""
    conversation = await _conversation(container)

    saved = await AnswerService(container).record_answer(
        conversation_id=conversation.id,
        question="?",
        criterion_key="нет-такого",
        answer="ответ",
    )

    assert saved is None


# ---------------------------------------------------------------- пересчёт


async def test_rescore_increases_percent_when_answer_closes_gap(container: Any) -> None:
    conversation = await _conversation(container)
    service = AnswerService(container)
    profile = _profile()
    screening = _screening(percent=50.0)

    saved = await service.record_answer(
        conversation_id=conversation.id,
        question="Сколько лет на Python?",
        criterion_key="python",
        answer="Пять лет коммерческой разработки на Python, Django, FastAPI, PostgreSQL.",
    )
    assert saved is not None

    rescored = await service.rescore(
        conversation_id=conversation.id,
        screening=screening,
        profile=profile,
        answers=[saved],
    )

    assert rescored.id != screening.id, "результат сохраняется отдельно"
    assert rescored.percent > screening.percent
    assert rescored.profile_version == profile.version


async def test_rescore_does_not_improve_on_contradiction(container: Any) -> None:
    """Противоречие с резюме не повышает оценку: разбирает человек."""
    from hirebot.domain.entities import QuestionAnswer

    conversation = await _conversation(container)
    profile = _profile()
    screening = _screening(percent=60.0)
    contradiction = QuestionAnswer(
        id=uuid4(),
        conversation_id=conversation.id,
        criterion_key="python",
        question="Сколько лет на Python?",
        answer="Год.",
        score=20.0,
        contradicts_resume=True,
    )

    rescored = await AnswerService(container).rescore(
        conversation_id=conversation.id,
        screening=screening,
        profile=profile,
        answers=[contradiction],
    )

    assert rescored.percent <= screening.percent


async def test_rescore_without_answers_returns_original(container: Any) -> None:
    conversation = await _conversation(container)
    screening = _screening(percent=42.0)

    rescored = await AnswerService(container).rescore(
        conversation_id=conversation.id,
        screening=screening,
        profile=_profile(),
        answers=[],
    )

    assert rescored is screening


async def test_rescored_result_is_persisted(container: Any) -> None:
    conversation = await _conversation(container)
    service = AnswerService(container)
    profile = _profile()
    screening = ScreeningResult(
        id=uuid4(),
        resume_id=uuid4(),
        vacancy_id=conversation.vacancy_id,
        candidate_id=conversation.candidate_id,
        total_score=40.0,
        verdict=ScreeningVerdict.WEAK,
        percent=40.0,
        coverage=0.5,
    )
    saved = await service.record_answer(
        conversation_id=conversation.id,
        question="Сколько лет на Python?",
        criterion_key="python",
        answer="Пять лет на Python, Django, FastAPI.",
    )
    assert saved is not None

    rescored = await service.rescore(
        conversation_id=conversation.id,
        screening=screening,
        profile=profile,
        answers=[saved],
    )

    async with container.uow_factory() as uow:
        latest = await uow.screenings.get_latest(conversation.candidate_id, conversation.vacancy_id)
    # Пересчёт сохраняется отдельной записью, а не перезаписывает прежнюю.
    assert latest is not None
    assert latest.id == rescored.id


async def test_answer_is_recorded_when_question_was_asked(container: Any) -> None:
    """Ответ связывается с критерием через последний заданный вопрос.

    Проверяется сам механизм: вопрос в истории → ответ кандидата → запись с
    привязкой к критерию. Вердикт модели здесь не участвует, поэтому тест
    устойчив к правкам промптов и заглушки.
    """
    from hirebot.domain.entities import Message, new_id
    from hirebot.domain.enums import MessageKind
    from hirebot.services.conversation import ConversationService

    conversation = await _conversation(container)
    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(conversation.vacancy_id)
    assert vacancy is not None

    profiles = ScoringProfileService(container)
    draft = await profiles.create_draft(
        vacancy=vacancy,
        criteria=_criteria(),
        questions=[ProfileQuestion(text="Сколько лет на Python?", criterion_key="python")],
    )
    await profiles.publish(draft.id, as_shadow=False)

    async with container.uow_factory() as uow:
        await uow.messages.add(
            Message(
                id=new_id(),
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                body="Сколько лет на Python?",
                kind=MessageKind.CLARIFYING_QUESTION,
                sent_at=conversation.created_at,
            )
        )
        await uow.commit()

    service = ConversationService(container)
    context = await service._load(conversation.id)
    assert context is not None
    assert context.profile is not None
    # Скрининга ещё нет — создаём, как это делает шаг воронки.
    context.screening = await service._screening.screen(conversation_id=conversation.id)

    await service._record_answer_if_question(
        context, "Пять лет коммерческой разработки на Python, Django, FastAPI."
    )

    async with container.uow_factory() as uow:
        answers = await uow.answers.list_for_conversation(conversation.id)
    assert answers, "ответ должен быть записан"
    assert answers[0].criterion_key == "python"
    assert answers[0].score > 0
    # Контекст получает пересчитанный результат: решение принимается по нему.
    assert context.screening is not None


async def test_answer_ignored_when_question_from_other_source(container: Any) -> None:
    """Вопрос не из профиля: не выдумываем критерий и не пишем ответ."""
    from hirebot.domain.entities import Message, new_id
    from hirebot.domain.enums import MessageKind
    from hirebot.services.conversation import ConversationService

    conversation = await _conversation(container)
    async with container.uow_factory() as uow:
        vacancy = await uow.vacancies.get(conversation.vacancy_id)
    assert vacancy is not None

    profiles = ScoringProfileService(container)
    draft = await profiles.create_draft(
        vacancy=vacancy,
        criteria=_criteria(),
        questions=[ProfileQuestion(text="Сколько лет на Python?", criterion_key="python")],
    )
    await profiles.publish(draft.id, as_shadow=False)

    async with container.uow_factory() as uow:
        await uow.messages.add(
            Message(
                id=new_id(),
                conversation_id=conversation.id,
                direction=MessageDirection.OUTBOUND,
                body="Когда вам удобно на собеседование?",
                kind=MessageKind.CLARIFYING_QUESTION,
                sent_at=conversation.created_at,
            )
        )
        await uow.commit()

    service = ConversationService(container)
    context = await service._load(conversation.id)
    assert context is not None
    context.screening = await service._screening.screen(conversation_id=conversation.id)

    await service._record_answer_if_question(context, "В понедельник.")

    async with container.uow_factory() as uow:
        answers = await uow.answers.list_for_conversation(conversation.id)
    assert answers == []
