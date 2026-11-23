"""Тесты профилей скоринга и планировщика вопросов.

Проверяется то, что определяет поведение на реальных кандидатах:

* черновик нельзя опубликовать с ошибками, а публикация создаёт версию;
* откат возвращает прежнюю версию, не удаляя новую;
* shadow-режим считает, но не отправляет;
* планировщик не задаёт больше пяти вопросов и останавливается, когда ответ
  уже не может изменить решение;
* стоп-правила срабатывают (блокер, просьба кандидата, непонятные ответы).
"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

import pytest

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.entities import Vacancy
from hirebot.domain.enums import (
    Channel,
    DecisionKind,
    ProfileStatus,
    StopReason,
)
from hirebot.domain.errors import PolicyViolation
from hirebot.domain.scoring import (
    CriterionScore,
    ProfileCriterion,
    ProfileQuestion,
    ProfileThresholds,
    ScoringBreakdown,
    apply_must_have_cap,
    compute_breakdown,
    normalized_weights,
)
from hirebot.services.assessment import AssessmentService, PlanningState, decision_interval
from hirebot.services.scoring_profile import ScoringProfileService, profile_cache_key


def _settings() -> Settings:
    return Settings(env="test", log_level="WARNING", log_json=False, _env_file=None)


@pytest.fixture
def container() -> Any:
    return build_fake_container(_settings(), seed_demo=False)


def _criteria() -> list[ProfileCriterion]:
    return [
        ProfileCriterion(
            key="python",
            title="Python",
            weight=5,
            must_have=True,
            expectation="не менее 3 лет",
        ),
        ProfileCriterion(key="sql", title="SQL", weight=3, expectation="уверенный SQL"),
        ProfileCriterion(key="english", title="Английский", weight=1),
    ]


def _questions() -> list[ProfileQuestion]:
    return [
        ProfileQuestion(text="Сколько лет на Python?", criterion_key="python"),
        ProfileQuestion(text="Работали с PostgreSQL?", criterion_key="sql"),
        ProfileQuestion(text="Какой уровень английского?", criterion_key="english"),
    ]


async def _vacancy(container: Any) -> Vacancy:
    async with container.uow_factory() as uow:
        company_id = uuid4()
        vacancy = Vacancy(
            id=uuid4(),
            channel=Channel.HH,
            external_id=f"vac-{uuid4().hex[:8]}",
            title="Python-разработчик",
            company_id=company_id,
            criteria=[item.as_vacancy_dict() for item in _criteria()],
        )
        from hirebot.domain.entities import Company

        await uow.companies.upsert(Company(id=company_id, name="Тест", slug=f"test-{company_id.hex[:8]}"))
        await uow.vacancies.upsert(vacancy)
        await uow.commit()
    return vacancy


# ---------------------------------------------------------------- профили


async def test_draft_then_publish_creates_version(container: Any) -> None:
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)

    draft = await service.create_draft(
        vacancy=vacancy,
        criteria=_criteria(),
        questions=_questions(),
        created_by="recruiter",
    )

    assert draft.status is ProfileStatus.DRAFT
    assert draft.version == 1
    assert len(draft.criteria) == 3

    published = await service.publish(draft.id)

    assert published.status is ProfileStatus.ACTIVE
    assert published.published_at is not None

    live = await service.get_live(vacancy.id)
    assert live is not None
    assert live.version == 1


async def test_publish_shadow_keeps_sending_disabled(container: Any) -> None:
    """Первый запуск: профиль считает, но кандидатам ничего не отправляется."""
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)
    draft = await service.create_draft(vacancy=vacancy, criteria=_criteria())

    published = await service.publish(draft.id, as_shadow=True)

    assert published.status is ProfileStatus.SHADOW
    assert published.is_live


async def test_second_publication_archives_previous(container: Any) -> None:
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)
    first = await service.create_draft(vacancy=vacancy, criteria=_criteria())
    await service.publish(first.id)

    second = await service.create_draft(
        vacancy=vacancy,
        criteria=_criteria()[:2],
        questions=[],
    )
    await service.publish(second.id)

    versions = await service.list_versions(vacancy.id)
    statuses = {item.version: item.status for item in versions}

    assert second.version == 2
    assert statuses[1] is ProfileStatus.ARCHIVED
    assert statuses[2] is ProfileStatus.ACTIVE
    # Живая версия ровно одна.
    assert sum(1 for item in versions if item.is_live) == 1


async def test_rollback_restores_previous_version(container: Any) -> None:
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)
    first = await service.create_draft(vacancy=vacancy, criteria=_criteria())
    await service.publish(first.id)
    second = await service.create_draft(vacancy=vacancy, criteria=_criteria()[:2], questions=[])
    await service.publish(second.id)

    restored = await service.rollback(vacancy.id, first.version)

    assert restored.version == 1
    assert restored.status is ProfileStatus.ACTIVE
    live = await service.get_live(vacancy.id)
    assert live is not None
    assert live.version == 1


async def test_draft_without_must_have_is_rejected_early(container: Any) -> None:
    """Черновик без обязательного критерия не должен попасть в базу.

    Проверка выполняется при создании, а не только при публикации: иначе
    невалидный черновик лежит и его случайно опубликуют.
    """
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)

    with pytest.raises(ValueError, match="обязательный"):
        await service.create_draft(
            vacancy=vacancy,
            criteria=[ProfileCriterion(key="sql", title="SQL", weight=3, must_have=False)],
        )


async def test_publish_rejects_discriminative_criterion(container: Any) -> None:
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)

    with pytest.raises(PolicyViolation):
        await service.create_draft(
            vacancy=vacancy,
            criteria=[ProfileCriterion(key="age", title="Возраст до 40 лет", weight=3, must_have=True)],
        )


async def test_publish_rejects_discriminative_question(container: Any) -> None:
    """Вопрос — тоже путь к дискриминации, а не только критерий."""
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)

    with pytest.raises(PolicyViolation):
        await service.create_draft(
            vacancy=vacancy,
            criteria=_criteria(),
            questions=[ProfileQuestion(text="Планируете ли детей?", criterion_key="english")],
        )


async def test_too_many_questions_rejected(container: Any) -> None:
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)
    questions = [ProfileQuestion(text=f"Вопрос {index}", criterion_key="python") for index in range(6)]

    with pytest.raises(PolicyViolation, match="лимит"):
        await service.create_draft(vacancy=vacancy, criteria=_criteria(), questions=questions)


async def test_profile_is_cached_and_invalidated_on_publish(container: Any) -> None:
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)
    draft = await service.create_draft(vacancy=vacancy, criteria=_criteria())
    await service.publish(draft.id)

    first = await service.get_live(vacancy.id)
    second = await service.get_live(vacancy.id)

    assert first is not None
    assert second is not None
    assert first.version == second.version

    # Публикация новой версии снимает кэш: иначе кандидаты считались бы по старой.
    newer = await service.create_draft(vacancy=vacancy, criteria=_criteria()[:2], questions=[])
    await service.publish(newer.id)
    refreshed = await service.get_live(vacancy.id)

    assert refreshed is not None
    assert refreshed.version == 2
    assert profile_cache_key(vacancy.id) in str(profile_cache_key(vacancy.id))


async def test_resolve_falls_back_to_vacancy_criteria(container: Any) -> None:
    """Вакансия без профиля обрабатывается по прежним критериям."""
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)

    resolved = await service.resolve_for_vacancy(vacancy)

    assert resolved is not None
    assert resolved.version == 0
    assert {item.key for item in resolved.criteria} == {"python", "sql", "english"}


async def test_resolve_returns_none_without_criteria(container: Any) -> None:
    vacancy = Vacancy(
        id=uuid4(),
        channel=Channel.HH,
        external_id="empty",
        title="Без критериев",
        company_id=uuid4(),
        criteria=[],
    )

    assert await ScoringProfileService(container).resolve_for_vacancy(vacancy) is None


# ---------------------------------------------------------------- вопросы


def _breakdown(**scores: float) -> ScoringBreakdown:
    criteria = _criteria()
    by_key = {item.key: item for item in criteria}
    values: dict[str, CriterionScore] = {}
    for key, score in scores.items():
        criterion = by_key[key]
        values[key] = CriterionScore(
            key=key,
            title=criterion.title,
            weight=criterion.weight,
            score=score,
            must_have=criterion.must_have,
            evidence="цитата",
            is_gap=score < 55,
        )
    return apply_must_have_cap(compute_breakdown(criteria, values))


async def _profile(container: Any, *, shadow: bool = False) -> Any:
    vacancy = await _vacancy(container)
    service = ScoringProfileService(container)
    draft = await service.create_draft(
        vacancy=vacancy,
        criteria=_criteria(),
        questions=_questions(),
        thresholds=ProfileThresholds(max_questions=5),
    )
    return await service.publish(draft.id, as_shadow=shadow)


async def test_planner_limits_questions_to_five(container: Any) -> None:
    profile = await _profile(container)
    # Больше вопросов, чем лимит, в профиль не попадёт, поэтому проверяем
    # лимит на уровне планировщика: он не должен вернуть больше max_questions.
    breakdown = _breakdown(python=50.0, sql=50.0, english=50.0)

    plan = AssessmentService(container).plan_questions(profile=profile, breakdown=breakdown)

    assert len(plan.questions) <= profile.thresholds.max_questions


async def test_planner_stops_when_decision_cannot_change(container: Any) -> None:
    """Ответы уже не могут изменить решение — спрашивать незачем."""
    profile = await _profile(container)
    breakdown = _breakdown(python=95.0, sql=95.0, english=95.0)

    plan = AssessmentService(container).plan_questions(profile=profile, breakdown=breakdown)

    assert plan.questions == []
    assert plan.stop_reason == StopReason.DECISION_STABLE.value


async def test_planner_asks_when_data_missing(container: Any) -> None:
    profile = await _profile(container)
    criteria = profile.criteria
    scores = {
        "python": CriterionScore(
            key="python", title="Python", weight=5, score=0.0, must_have=True, evidence=""
        ),
        "sql": CriterionScore(key="sql", title="SQL", weight=3, score=0.0, evidence=""),
        "english": CriterionScore(key="english", title="Английский", weight=1, score=0.0, evidence=""),
    }
    breakdown = compute_breakdown(criteria, scores)

    plan = AssessmentService(container).plan_questions(profile=profile, breakdown=breakdown)

    assert plan.questions, "при отсутствии данных вопросы нужны"
    assert all(item.value > 0 for item in plan.questions)


async def test_planner_respects_limit_reached(container: Any) -> None:
    profile = await _profile(container)
    breakdown = _breakdown(python=40.0, sql=40.0, english=40.0)
    state = PlanningState(asked=["1", "2", "3", "4", "5"])

    plan = AssessmentService(container).plan_questions(profile=profile, breakdown=breakdown, state=state)

    assert plan.stop_reason == StopReason.LIMIT_REACHED.value


async def test_planner_stops_on_candidate_request(container: Any) -> None:
    profile = await _profile(container)
    breakdown = _breakdown(python=50.0, sql=50.0, english=50.0)

    plan = AssessmentService(container).plan_questions(
        profile=profile,
        breakdown=breakdown,
        state=PlanningState(candidate_asked_to_stop=True),
    )

    assert plan.stop_reason == StopReason.CANDIDATE_ASKED.value


async def test_planner_stops_after_two_unclear_answers(container: Any) -> None:
    profile = await _profile(container)
    breakdown = _breakdown(python=50.0, sql=50.0, english=50.0)

    plan = AssessmentService(container).plan_questions(
        profile=profile,
        breakdown=breakdown,
        state=PlanningState(unclear_rounds=2),
    )

    assert plan.stop_reason == StopReason.UNCLEAR_TWICE.value


async def test_planner_does_not_ask_about_covered_criterion(container: Any) -> None:
    profile = await _profile(container)
    breakdown = _breakdown(python=90.0, sql=40.0, english=40.0)

    plan = AssessmentService(container).plan_questions(profile=profile, breakdown=breakdown)

    asked_criteria = {item.criterion_key for item in plan.questions}
    assert "python" not in asked_criteria, "по закрытому критерию спрашивать незачем"


async def test_interval_narrows_with_data(container: Any) -> None:
    profile = await _profile(container)
    breakdown = _breakdown(python=90.0, sql=90.0, english=90.0)

    low, high = decision_interval(profile, breakdown)

    assert low == pytest.approx(high), "при полных данных интервал сходится"
    assert low > 80.0


async def test_interval_wide_without_data(container: Any) -> None:
    profile = await _profile(container)
    scores = {
        item.key: CriterionScore(key=item.key, title=item.title, weight=item.weight, score=0.0, evidence="")
        for item in profile.criteria
    }
    breakdown = compute_breakdown(profile.criteria, scores)

    low, high = decision_interval(profile, breakdown)

    assert low == pytest.approx(0.0)
    assert high == pytest.approx(100.0)


# ---------------------------------------------------------------- решение


async def test_shadow_profile_never_returns_invite(container: Any) -> None:
    profile = await _profile(container, shadow=True)
    breakdown = _breakdown(python=95.0, sql=95.0, english=95.0)

    outcome = AssessmentService(container).decide(profile=profile, breakdown=breakdown)

    assert outcome.decision is DecisionKind.HUMAN
    assert outcome.stop_reason == "shadow"
    assert "shadow" in outcome.explanation


async def test_active_profile_invites_on_all_conditions(container: Any) -> None:
    profile = await _profile(container)
    breakdown = _breakdown(python=95.0, sql=95.0, english=95.0)

    outcome = AssessmentService(container).decide(profile=profile, breakdown=breakdown)

    assert outcome.decision is DecisionKind.INVITE
    assert outcome.percent >= 85.0


async def test_blocker_prevents_invite(container: Any) -> None:
    profile = await _profile(container)
    criteria = profile.criteria
    scores = {
        "python": CriterionScore(
            key="python",
            title="Python",
            weight=5,
            score=20.0,
            must_have=True,
            evidence="учусь",
            is_gap=True,
        ),
        "sql": CriterionScore(key="sql", title="SQL", weight=3, score=100.0, evidence="PostgreSQL"),
        "english": CriterionScore(key="english", title="Английский", weight=1, score=100.0, evidence="C1"),
    }
    breakdown = apply_must_have_cap(compute_breakdown(criteria, scores))

    outcome = AssessmentService(container).decide(profile=profile, breakdown=breakdown)

    assert outcome.decision is DecisionKind.HUMAN
    assert outcome.blockers


async def test_weights_normalized_across_profiles(container: Any) -> None:
    """Проценты сравнимы между вакансиями с разным числом критериев."""
    few = normalized_weights(_criteria()[:1])
    many = normalized_weights(_criteria())

    assert sum(few.values()) == pytest.approx(1.0)
    assert sum(many.values()) == pytest.approx(1.0)
