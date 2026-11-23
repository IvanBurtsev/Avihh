"""Тесты предскоринга: экономия без потери качества.

Предскоринг решает, стоит ли **платно** открывать резюме. Ошибки здесь дороги
в обе стороны:

* слишком строго — отсекаем хороших кандидатов;
* слишком мягко — тратим квоту на явно нерелевантных.

Поэтому проверяется не «красивое число», а поведение: при отсутствии данных
не блокировать, при явном несовпадении — пропустить открытие.
"""

from __future__ import annotations

from uuid import uuid4

import pytest

from hirebot.domain.entities import Vacancy
from hirebot.domain.enums import Channel, ProfileStatus
from hirebot.domain.scoring import ProfileCriterion, ScoringProfile
from hirebot.ports.dto import InboundResponse
from hirebot.services.prefilter import (
    PrefilterSettings,
    PreliminaryScore,
    score_response,
    should_skip_resume,
)


def _vacancy() -> Vacancy:
    return Vacancy(
        id=uuid4(),
        channel=Channel.HH,
        external_id="vac-1",
        title="Python-разработчик",
        company_id=uuid4(),
    )


def _profile() -> ScoringProfile:
    return ScoringProfile(
        id=uuid4(),
        company_id=uuid4(),
        vacancy_id=uuid4(),
        version=1,
        status=ProfileStatus.ACTIVE,
        criteria=[
            ProfileCriterion(
                key="python",
                title="Python",
                weight=5,
                must_have=True,
                expectation="коммерческая разработка на Python",
                evidence_hint="python, django, fastapi",
            ),
            ProfileCriterion(
                key="sql",
                title="SQL и PostgreSQL",
                weight=3,
                expectation="уверенный SQL",
                evidence_hint="postgresql, sql",
            ),
        ],
    )


def _response(cover_letter: str = "", **raw: str) -> InboundResponse:
    return InboundResponse(
        channel=Channel.HH,
        conversation_external_id="conv-1",
        candidate_external_id="cand-1",
        vacancy_external_id="vac-1",
        cover_letter=cover_letter,
        raw=dict(raw),
    )


def test_relevant_letter_scores_high() -> None:
    result = score_response(
        vacancy=_vacancy(),
        response=_response(
            "Пишу на Python пять лет, работал с Django и FastAPI, уверенно владею PostgreSQL и SQL."
        ),
        profile=_profile(),
    )

    assert result.has_data
    assert result.score > 50
    assert "python" in result.matched


def test_irrelevant_letter_scores_low() -> None:
    result = score_response(
        vacancy=_vacancy(),
        response=_response("Ищу работу водителем, есть права категории C."),
        profile=_profile(),
    )

    assert result.has_data
    assert result.score < 20


def test_empty_data_means_no_decision() -> None:
    """Нет письма и метаданных — решать не по чему, и это не «плохой кандидат»."""
    result = score_response(vacancy=_vacancy(), response=_response(), profile=_profile())

    assert result.has_data is False
    assert should_skip_resume(preliminary=result) is False


def test_metadata_only_gives_some_signal() -> None:
    result = score_response(
        vacancy=_vacancy(),
        response=_response(title="Python developer, SQL"),
        profile=_profile(),
    )

    assert result.has_data
    assert result.score > 0


def test_weak_result_skips_resume_view() -> None:
    result = score_response(
        vacancy=_vacancy(),
        response=_response("Продавец-консультант, опыт в рознице."),
        profile=_profile(),
        settings=PrefilterSettings(enabled=True, min_score=20.0),
    )

    assert should_skip_resume(preliminary=result, settings=PrefilterSettings(enabled=True, min_score=20.0))


def test_strong_result_opens_resume() -> None:
    result = score_response(
        vacancy=_vacancy(),
        response=_response("Python, FastAPI, PostgreSQL, SQL — шесть лет опыта."),
        profile=_profile(),
        settings=PrefilterSettings(enabled=True, min_score=20.0),
    )

    assert not should_skip_resume(
        preliminary=result, settings=PrefilterSettings(enabled=True, min_score=20.0)
    )


def test_disabled_prefilter_never_skips() -> None:
    result = PreliminaryScore(score=0.0, has_data=True)

    assert should_skip_resume(preliminary=result, settings=PrefilterSettings(enabled=False)) is False


def test_profile_without_criteria_falls_back_to_vacancy_title() -> None:
    result = score_response(
        vacancy=_vacancy(),
        response=_response("Python разработчик, ищу проект"),
        profile=None,
    )

    assert result.has_data


def test_vacancy_without_any_tokens_gives_no_data() -> None:
    empty = Vacancy(
        id=uuid4(),
        channel=Channel.HH,
        external_id="v-2",
        title="",
        company_id=uuid4(),
    )

    result = score_response(vacancy=empty, response=_response("текст"), profile=None)

    assert result.has_data is False


def test_letter_is_truncated_for_analysis() -> None:
    """Огромное письмо не анализируется целиком: сигнала больше не становится."""
    long_letter = ("Python " * 5000) + "конец"
    result = score_response(vacancy=_vacancy(), response=_response(long_letter), profile=_profile())

    assert result.has_data
    assert result.score > 0


@pytest.mark.parametrize("min_score", [0.0, 20.0, 50.0])
def test_threshold_changes_decision(min_score: float) -> None:
    """Порог — настройка: при разных значениях решение меняется предсказуемо."""
    result = score_response(
        vacancy=_vacancy(),
        response=_response("Python, немного SQL."),
        profile=_profile(),
    )
    settings = PrefilterSettings(enabled=True, min_score=min_score)

    skip = should_skip_resume(preliminary=result, settings=settings)

    assert skip == (result.score < min_score)
