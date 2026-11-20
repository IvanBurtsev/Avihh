"""Тесты скрининга: пересчёт балла, кэш, деградация, защита от галлюцинаций."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from services_support import (
    BORDERLINE_CRITERIA,
    DEMO_RESUME_STRONG,
    DEMO_RESUME_WEAK,
    MUST_HAVE_CRITERIA,
    seed_pipeline,
    seed_response,
    seed_vacancy,
)

from hirebot.container import Container
from hirebot.domain.entities import ScreeningCriterion
from hirebot.domain.enums import Channel, ConversationState, ScreeningVerdict
from hirebot.domain.errors import NotFound, PolicyViolation
from hirebot.ports.llm import ScreeningDraft
from hirebot.services.ingest import IngestService
from hirebot.services.screening import (
    MUST_HAVE_SCORE_CAP,
    NO_RESUME_ID,
    ScreeningService,
    evaluate_draft,
    redact_pii,
    weighted_score,
)
from hirebot.testing.fakes import FakeLlmAdapter


def _criterion(
    key: str,
    score: float,
    *,
    weight: float = 1.0,
    must_have: bool = False,
) -> ScreeningCriterion:
    return ScreeningCriterion(
        key=key,
        title=key,
        weight=weight,
        score=score,
        must_have=must_have,
        evidence="цитата" if score >= 50 else "",
        is_gap=score < 50,
    )


async def _conversation_id(container: Container) -> UUID:
    await IngestService(container).poll(Channel.HH)
    async with container.uow_factory() as uow:
        return next(iter(uow.store.conversations))


# --------------------------------------------------------------------------- #
# Чистая математика скоринга
# --------------------------------------------------------------------------- #


def test_weighted_score_uses_weights() -> None:
    """Итоговый балл — взвешенное среднее, а не среднее арифметическое."""
    criteria = [_criterion("a", 100.0, weight=3.0), _criterion("b", 0.0, weight=1.0)]
    assert weighted_score(criteria) == 75.0


def test_weighted_score_falls_back_to_mean_without_weights() -> None:
    criteria = [_criterion("a", 80.0, weight=0.0), _criterion("b", 40.0, weight=0.0)]
    assert weighted_score(criteria) == 60.0


def test_model_total_score_is_ignored() -> None:
    """Модель может «нарисовать» 100 баллов — считаем сами."""
    draft = ScreeningDraft(
        total_score=100.0,
        criteria=[
            _criterion("python", 20.0, weight=1.0),
            _criterion("sql", 40.0, weight=1.0),
        ],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=70.0)
    assert outcome.score == 30.0
    assert outcome.verdict is ScreeningVerdict.REJECT


def test_high_score_without_must_have_gaps_is_strong_match() -> None:
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[_criterion("python", 95.0, weight=2.0), _criterion("sql", 90.0, weight=1.0)],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=70.0)
    assert outcome.score > 70.0
    assert outcome.verdict is ScreeningVerdict.STRONG_MATCH


def test_borderline_score_is_weak_not_reject() -> None:
    """Пограничный балл уходит человеку, а не в отказ."""
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[_criterion("python", 65.0), _criterion("sql", 62.0)],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=70.0)
    assert 55.0 < outcome.score < 70.0
    assert outcome.verdict is ScreeningVerdict.WEAK


def test_must_have_gap_caps_score_and_verdict() -> None:
    """Провал обязательного критерия — потолок балла и вердикт не выше WEAK."""
    draft = ScreeningDraft(
        total_score=0.0,
        criteria=[
            _criterion("python", 98.0, weight=10.0),
            _criterion("k8s", 15.0, weight=1.0, must_have=True),
        ],
    )
    outcome = evaluate_draft(draft, min_score_to_invite=70.0)
    # Средний балл высокий (89.5), но обязательный критерий провален.
    assert weighted_score(outcome.criteria) > 70.0
    assert outcome.score == MUST_HAVE_SCORE_CAP
    assert outcome.verdict is ScreeningVerdict.WEAK
    assert [criterion.key for criterion in outcome.criteria if criterion.must_have and criterion.is_gap] == [
        "k8s"
    ]


def test_low_must_have_score_becomes_gap_even_without_flag() -> None:
    criterion = ScreeningCriterion(
        key="k8s",
        title="Kubernetes",
        weight=1.0,
        score=30.0,
        must_have=True,
        is_gap=False,
    )
    outcome = evaluate_draft(ScreeningDraft(total_score=0.0, criteria=[criterion]), min_score_to_invite=70.0)
    assert outcome.criteria[0].is_gap is True
    assert outcome.verdict is ScreeningVerdict.WEAK


def test_redact_pii_removes_contacts_and_keeps_years() -> None:
    text = "Опыт 5 лет. Почта: a@b.ru, тел. +7 999 123-45-67. Работал с 2019 по 2024 год."
    cleaned = redact_pii(text)
    assert "a@b.ru" not in cleaned
    assert "999" not in cleaned
    assert "5 лет" in cleaned
    assert "2019 по 2024" in cleaned


# --------------------------------------------------------------------------- #
# Сервис скрининга
# --------------------------------------------------------------------------- #


async def test_strong_resume_is_screened_and_saved(container: Container) -> None:
    await seed_pipeline(container)
    conversation_id = await _conversation_id(container)

    result = await ScreeningService(container).screen(conversation_id=conversation_id)

    assert result.verdict in {ScreeningVerdict.STRONG_MATCH, ScreeningVerdict.MATCH}
    assert result.total_score >= 70.0
    assert result.must_have_failures == []
    assert result.cache_hit is False
    assert result.model != ""
    # Версия промпта обязана попадать в аудит, но её конкретное значение —
    # предмет изменения промптов: привязываться к номеру в тесте нельзя.
    assert result.prompt_version
    assert result.model
    assert result.summary != ""

    async with container.uow_factory() as uow:
        assert len(uow.store.screenings) == 1


async def test_screening_is_cached_by_content_hash(container: Container) -> None:
    """Одно и то же резюме не оплачивается модели дважды."""
    await seed_pipeline(container)
    conversation_id = await _conversation_id(container)
    service = ScreeningService(container)
    llm = container.llm
    assert isinstance(llm, FakeLlmAdapter)

    first = await service.screen(conversation_id=conversation_id)
    second = await service.screen(conversation_id=conversation_id)

    assert first.cache_hit is False
    assert second.cache_hit is True
    assert second.total_score == first.total_score
    assert len(llm.screen_calls) == 1


async def test_resume_access_denied_returns_needs_review(container: Container) -> None:
    """Нет прав на резюме — это штатная деградация, а не падение цикла."""
    await seed_pipeline(container, deny_resume=True)
    conversation_id = await _conversation_id(container)

    result = await ScreeningService(container).screen(conversation_id=conversation_id)

    assert result.verdict is ScreeningVerdict.NEEDS_REVIEW
    assert result.resume_id == NO_RESUME_ID
    assert "Резюме недоступно" in result.summary
    async with container.uow_factory() as uow:
        assert uow.store.screenings == {}


async def test_pii_is_redacted_before_llm_when_send_pii_disabled(container: Container) -> None:
    resume_text = DEMO_RESUME_STRONG + "\nТелефон: +7 916 000-11-22, почта: test@example.com"
    await seed_pipeline(container, resume_text=resume_text)
    conversation_id = await _conversation_id(container)
    llm = container.llm
    assert isinstance(llm, FakeLlmAdapter)

    await ScreeningService(container).screen(conversation_id=conversation_id)

    sent_text = llm.screen_calls[0].resume_text
    assert "test@example.com" not in sent_text
    assert "916" not in sent_text
    assert "5 лет" in sent_text


async def test_protected_criterion_stops_screening(container: Container) -> None:
    """Критерий по возрасту не уходит в модель: это запрещено политикой."""
    await seed_pipeline(
        container,
        criteria=(
            {"key": "python", "title": "Опыт разработки на Python", "weight": 1.0, "must_have": False},
            {"key": "age", "title": "Возраст кандидата", "weight": 1.0, "must_have": False},
        ),
    )
    conversation_id = await _conversation_id(container)
    llm = container.llm
    assert isinstance(llm, FakeLlmAdapter)

    with pytest.raises(PolicyViolation, match="защищённый признак"):
        await ScreeningService(container).screen(conversation_id=conversation_id)

    assert llm.screen_calls == []


async def test_vacancy_without_criteria_needs_human(container: Container) -> None:
    """Без критериев оценивать нечего — честный NEEDS_REVIEW, а не «балл на глаз»."""
    await seed_pipeline(container, criteria=())
    conversation_id = await _conversation_id(container)

    result = await ScreeningService(container).screen(conversation_id=conversation_id)

    assert result.verdict is ScreeningVerdict.NEEDS_REVIEW
    assert "критерии" in result.summary


async def test_weak_resume_gets_gap_and_low_score(container: Container) -> None:
    await seed_pipeline(container, resume_text=DEMO_RESUME_WEAK)
    conversation_id = await _conversation_id(container)

    result = await ScreeningService(container).screen(conversation_id=conversation_id)

    assert result.verdict is ScreeningVerdict.WEAK
    assert result.total_score <= MUST_HAVE_SCORE_CAP
    assert [criterion.key for criterion in result.must_have_failures] == ["python"]


async def test_borderline_candidate_scores_near_threshold(container: Container) -> None:
    await seed_pipeline(container, criteria=BORDERLINE_CRITERIA)
    conversation_id = await _conversation_id(container)

    result = await ScreeningService(container).screen(conversation_id=conversation_id)

    assert result.verdict is ScreeningVerdict.WEAK
    assert 55.0 < result.total_score < 70.0


async def test_must_have_criterion_produces_questions(container: Container) -> None:
    await seed_pipeline(container, criteria=MUST_HAVE_CRITERIA)
    conversation_id = await _conversation_id(container)

    result = await ScreeningService(container).screen(conversation_id=conversation_id)

    assert result.questions
    assert "Kubernetes" in result.gaps[0]


async def test_cached_result_is_attributed_to_other_candidate(container: Container) -> None:
    """Совпал текст резюме — оценки переиспользуются, но привязываются к своему кандидату."""
    await seed_pipeline(container)
    first_conversation = await _conversation_id(container)
    service = ScreeningService(container)
    first = await service.screen(conversation_id=first_conversation)

    await seed_vacancy(container)
    await seed_response(
        container,
        conversation_external_id="hh-chat-test-2",
        candidate_external_id="hh-cand-test-2",
        resume_external_id="hh-resume-test-2",
    )
    await IngestService(container).poll(Channel.HH)
    async with container.uow_factory() as uow:
        second_conversation = next(
            conversation
            for conversation in uow.store.conversations.values()
            if conversation.external_id == "hh-chat-test-2"
        )

    second = await service.screen(conversation_id=second_conversation.id)

    assert second.cache_hit is True
    assert second.total_score == first.total_score
    assert second.candidate_id == second_conversation.candidate_id
    assert second.candidate_id != first.candidate_id


async def test_screen_requires_existing_conversation(container: Container) -> None:
    with pytest.raises(NotFound):
        await ScreeningService(container).screen(conversation_id=uuid4())


async def test_conversation_state_is_not_changed_by_screening(container: Container) -> None:
    """Скрининг только считает: состоянием диалога управляет оркестратор."""
    await seed_pipeline(container)
    conversation_id = await _conversation_id(container)

    await ScreeningService(container).screen(conversation_id=conversation_id)

    async with container.uow_factory() as uow:
        conversation = uow.store.conversations[conversation_id]
        assert conversation.state is ConversationState.NEW
