"""Тесты приоритизации: кому писать первым.

Проверяется, что приоритет объясним и не превращается в «магическую» поправку:

* вклад каждого фактора виден и суммируется;
* свежий отклик обгоняет старый при равном соответствии;
* неизвестная готовность выйти не наказывает кандидата;
* блокер не вычитает балл, а помечает кандидата как «нужен человек»;
* очередь сортируется по приоритету, а не по дате отклика.
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.entities import ScreeningResult
from hirebot.domain.enums import Channel, ScreeningVerdict
from hirebot.domain.scoring import DEFAULT_PRIORITY_WEIGHTS
from hirebot.services.priority import PriorityService


@pytest.fixture
def container() -> Any:
    return build_fake_container(Settings(env="test", log_level="WARNING", _env_file=None))


def _screening(*, percent: float, coverage: float = 1.0, blockers: list[str] | None = None) -> Any:
    return ScreeningResult(
        id=uuid4(),
        resume_id=uuid4(),
        vacancy_id=uuid4(),
        candidate_id=uuid4(),
        total_score=percent,
        verdict=ScreeningVerdict.MATCH,
        percent=percent,
        coverage=coverage,
        blockers=blockers or [],
    )


def test_priority_is_weighted_sum_of_contributions(container: Any) -> None:
    service = PriorityService(container)

    breakdown = service.breakdown_for(screening=_screening(percent=90.0))

    expected = (
        DEFAULT_PRIORITY_WEIGHTS["percent"] * 90.0
        + DEFAULT_PRIORITY_WEIGHTS["coverage"] * 100.0
        + DEFAULT_PRIORITY_WEIGHTS["freshness"] * 100.0
        + DEFAULT_PRIORITY_WEIGHTS["availability"] * 50.0
    )
    assert breakdown.score == pytest.approx(expected, abs=0.1)
    assert sum(breakdown.contributions().values()) == pytest.approx(breakdown.score, abs=0.1)


def test_fresh_application_beats_old_at_equal_match(container: Any) -> None:
    service = PriorityService(container)
    now = service.now()

    fresh = service.breakdown_for(screening=_screening(percent=80.0), received_at=now - timedelta(minutes=5))
    old = service.breakdown_for(screening=_screening(percent=80.0), received_at=now - timedelta(days=3))

    assert fresh.score > old.score
    assert fresh.contributions()["freshness"] > old.contributions()["freshness"]


def test_unknown_availability_is_neutral(container: Any) -> None:
    """Отсутствие данных о готовности не должно понижать приоритет."""
    service = PriorityService(container)

    unknown = service.breakdown_for(screening=_screening(percent=70.0))
    ready_now = service.breakdown_for(screening=_screening(percent=70.0), ready_days=0)

    assert ready_now.score > unknown.score
    # Но неизвестность не обнуляет вклад: половина шкалы.
    assert unknown.contributions()["availability"] > 0


def test_blocker_is_flagged_not_subtracted(container: Any) -> None:
    """Блокер не «вычитает 40»: он помечает кандидата, балл считается как есть."""
    service = PriorityService(container)

    blocked = service.breakdown_for(screening=_screening(percent=90.0, blockers=["Python"]))
    clean = service.breakdown_for(screening=_screening(percent=90.0))

    assert blocked.has_blocker is True
    assert blocked.score == pytest.approx(clean.score)


def test_custom_weights_change_order(container: Any) -> None:
    """Коэффициенты — настройка, а не константа: их изменение меняет приоритет."""
    service = PriorityService(container)
    screening = _screening(percent=50.0, coverage=1.0)

    percent_heavy = service.breakdown_for(
        screening=screening, weights={"percent": 1.0, "coverage": 0.0, "freshness": 0.0, "availability": 0.0}
    )
    coverage_heavy = service.breakdown_for(
        screening=screening, weights={"percent": 0.0, "coverage": 1.0, "freshness": 0.0, "availability": 0.0}
    )

    assert percent_heavy.score == pytest.approx(50.0)
    assert coverage_heavy.score == pytest.approx(100.0)


def test_no_screening_gives_zero_percent(container: Any) -> None:
    service = PriorityService(container)

    breakdown = service.breakdown_for(screening=None)

    assert breakdown.percent == 0.0
    assert breakdown.coverage == 0.0


def test_queue_sorts_by_priority_not_by_date(container: Any) -> None:
    """Очередь ставит сильного кандидата выше раннего, но слабого."""
    import asyncio

    from hirebot.services import ConversationService, IngestService

    async def scenario() -> list[Any]:
        await IngestService(container).poll(Channel.HH)
        async with container.uow_factory() as uow:
            conversations = await uow.conversations.list_by_state(
                list(__import__("hirebot.domain.enums", fromlist=["ConversationState"]).ConversationState),
                limit=10,
            )
        for conversation in conversations:
            await ConversationService(container).process(conversation.id)
        return await PriorityService(container).queue(company_id=conversations[0].company_id, limit=10)

    items = asyncio.run(scenario())

    assert items, "демо-данные должны дать хотя бы одного кандидата"
    priorities = [item.priority for item in items]
    assert priorities == sorted(priorities, reverse=True), "очередь обязана быть отсортирована"


def test_queue_items_explain_priority(container: Any) -> None:
    import asyncio

    from hirebot.services import ConversationService, IngestService

    async def scenario() -> list[Any]:
        await IngestService(container).poll(Channel.HH)
        async with container.uow_factory() as uow:
            conversations = await uow.conversations.list_by_state(
                list(__import__("hirebot.domain.enums", fromlist=["ConversationState"]).ConversationState),
                limit=10,
            )
        for conversation in conversations:
            await ConversationService(container).process(conversation.id)
        return await PriorityService(container).queue(company_id=conversations[0].company_id, limit=10)

    items = asyncio.run(scenario())

    assert items
    for item in items:
        assert set(item.contributions) == {"percent", "coverage", "freshness", "availability"}
        assert 0.0 <= item.priority <= 100.0
        assert item.percent >= 0.0
