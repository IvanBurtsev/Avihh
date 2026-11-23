"""Тесты бюджета платных действий.

Проверяется то, что защищает от неожиданного счёта:

* дневной и месячный лимиты блокируют действие **до** вызова площадки;
* бесплатное действие проходит всегда;
* демо-канал не списывает (иначе локальная проверка упиралась бы в лимиты);
* запись списания увеличивает счётчик;
* неизвестное действие не считается бесплатным;
* мягкий режим не блокирует, но решение остаётся «нельзя».
"""

from __future__ import annotations

from datetime import timedelta
from typing import Any
from uuid import uuid4

import pytest

from hirebot.config import Settings
from hirebot.container import build_fake_container
from hirebot.domain.budget import (
    DEFAULT_COSTS,
    BudgetLimits,
    BudgetUsage,
    PaidActionKind,
    decide,
)
from hirebot.domain.enums import Channel
from hirebot.domain.errors import BudgetExceeded
from hirebot.services.base import DEFAULT_COMPANY_ID
from hirebot.services.budget import BudgetService


@pytest.fixture
def container() -> Any:
    return build_fake_container(Settings(env="test", log_level="WARNING", _env_file=None))


def _usage(spent_today: float = 0.0, spent_month: float = 0.0) -> BudgetUsage:
    return BudgetUsage(
        spent_today=spent_today,
        spent_month=spent_month,
        daily_limit=10,
        monthly_limit=100,
    )


# ---------------------------------------------------------------- чистое правило


def test_daily_limit_blocks_action() -> None:
    decision = decide(
        limits=BudgetLimits(daily_limit=10, monthly_limit=100),
        usage=_usage(spent_today=10),
        kind=PaidActionKind.RESUME_VIEW,
    )

    assert decision.blocked
    assert "дневной лимит" in decision.reason


def test_daily_limit_counts_cost_of_action() -> None:
    """Лимит 10, потрачено 9, действие стоит 1 — помещается; два — нет."""
    limits = BudgetLimits(daily_limit=10, monthly_limit=100, costs={"resume_view": 2.0})

    fits = decide(limits=limits, usage=_usage(spent_today=8), kind=PaidActionKind.RESUME_VIEW)
    exceeds = decide(limits=limits, usage=_usage(spent_today=9), kind=PaidActionKind.RESUME_VIEW)

    assert fits.allowed
    assert exceeds.blocked


def test_monthly_limit_checked_first() -> None:
    decision = decide(
        limits=BudgetLimits(daily_limit=10, monthly_limit=100),
        usage=BudgetUsage(spent_today=0.0, spent_month=100.0, daily_limit=10, monthly_limit=100),
        kind=PaidActionKind.RESUME_VIEW,
    )

    assert decision.blocked
    assert "месячный лимит" in decision.reason


def test_free_action_always_allowed() -> None:
    decision = decide(
        limits=BudgetLimits(daily_limit=1, monthly_limit=1),
        usage=_usage(spent_today=1, spent_month=1),
        kind=PaidActionKind.MESSAGE,
    )

    assert decision.allowed
    assert decision.cost == 0.0


def test_unknown_action_is_not_free() -> None:
    """Неизвестное действие не должно выглядеть бесплатным."""
    limits = BudgetLimits(costs={})

    assert limits.cost_of(PaidActionKind.INVITE) > 0


def test_disabled_budget_allows_everything() -> None:
    decision = decide(
        limits=BudgetLimits(enabled=False, daily_limit=1, monthly_limit=1),
        usage=_usage(spent_today=5, spent_month=50),
        kind=PaidActionKind.RESUME_VIEW,
    )

    assert decision.allowed
    assert "выключена" in decision.reason


def test_soft_stop_returns_not_allowed_without_raising() -> None:
    """Мягкий режим: решение «нельзя», но исключение не бросается."""
    decision = decide(
        limits=BudgetLimits(daily_limit=5, monthly_limit=100, hard_stop=False),
        usage=_usage(spent_today=5),
        kind=PaidActionKind.RESUME_VIEW,
    )

    assert decision.blocked


def test_limits_reject_invalid_configuration() -> None:
    with pytest.raises(ValueError, match="Дневной лимит"):
        BudgetLimits(daily_limit=200, monthly_limit=100)


# ---------------------------------------------------------------- сервис


async def test_demo_channel_is_not_chargeable(container: Any) -> None:
    """Демо-адаптер не списывает: иначе тесты и локальная проверка встанут."""
    service = BudgetService(container)

    assert service.is_chargeable(Channel.HH) is False


async def test_record_increases_usage(container: Any) -> None:
    service = BudgetService(container)
    before = await service.usage(DEFAULT_COMPANY_ID)

    await service.record(
        company_id=DEFAULT_COMPANY_ID,
        channel=Channel.HH,
        kind=PaidActionKind.RESUME_VIEW,
        reference="resume-1",
    )

    after = await service.usage(DEFAULT_COMPANY_ID)
    assert after.spent_today == pytest.approx(before.spent_today + 1.0)
    assert after.spent_month == pytest.approx(before.spent_month + 1.0)


async def test_record_with_custom_cost(container: Any) -> None:
    service = BudgetService(container)

    await service.record(
        company_id=DEFAULT_COMPANY_ID,
        channel=Channel.AVITO,
        kind=PaidActionKind.INVITE,
        cost=2.5,
    )

    usage = await service.usage(DEFAULT_COMPANY_ID)
    assert usage.spent_today == pytest.approx(2.5)


async def test_ensure_allowed_skips_demo_channel(container: Any) -> None:
    """Демо-канал проходит без проверки лимита."""
    service = BudgetService(container)

    decision = await service.ensure_allowed(
        company_id=DEFAULT_COMPANY_ID,
        channel=Channel.HH,
        kind=PaidActionKind.RESUME_VIEW,
    )

    assert decision.allowed
    assert "не списывает" in decision.reason


async def test_ensure_allowed_raises_on_hard_stop(container: Any) -> None:
    """Реальный канал при исчерпанном лимите блокируется исключением."""
    container.settings.budget.daily_limit = 1
    container.settings.budget.monthly_limit = 100
    container.settings.budget.hard_stop = True
    service = BudgetService(container)

    class _RealChannel:
        is_fake = False

    container.channels[Channel.HH] = _RealChannel()  # type: ignore[assignment]

    await service.record(
        company_id=DEFAULT_COMPANY_ID,
        channel=Channel.HH,
        kind=PaidActionKind.RESUME_VIEW,
    )

    with pytest.raises(BudgetExceeded, match="дневной лимит"):
        await service.ensure_allowed(
            company_id=DEFAULT_COMPANY_ID,
            channel=Channel.HH,
            kind=PaidActionKind.RESUME_VIEW,
        )


async def test_ensure_allowed_soft_mode_does_not_raise(container: Any) -> None:
    container.settings.budget.daily_limit = 1
    container.settings.budget.monthly_limit = 100
    container.settings.budget.hard_stop = False
    service = BudgetService(container)

    class _RealChannel:
        is_fake = False

    container.channels[Channel.HH] = _RealChannel()  # type: ignore[assignment]
    await service.record(
        company_id=DEFAULT_COMPANY_ID,
        channel=Channel.HH,
        kind=PaidActionKind.RESUME_VIEW,
    )

    decision = await service.ensure_allowed(
        company_id=DEFAULT_COMPANY_ID,
        channel=Channel.HH,
        kind=PaidActionKind.RESUME_VIEW,
    )

    assert decision.blocked


async def test_spending_is_isolated_per_company(container: Any) -> None:
    """Расход одной компании не блокирует другую."""
    service = BudgetService(container)
    other = uuid4()

    await service.record(company_id=other, channel=Channel.HH, kind=PaidActionKind.RESUME_VIEW)

    mine = await service.usage(DEFAULT_COMPANY_ID)
    theirs = await service.usage(other)

    assert mine.spent_today == 0.0
    assert theirs.spent_today == pytest.approx(1.0)


async def test_usage_ignores_records_from_previous_month(container: Any) -> None:
    """Списания прошлого месяца не входят ни в дневной, ни в месячный лимит."""
    service = BudgetService(container)
    now = service.now()
    # Гарантированно прошлый месяц: первое число текущего месяца минус сутки.
    previous_month = now.replace(day=1, hour=12, minute=0, second=0, microsecond=0)
    previous_month = previous_month - timedelta(days=1)

    from hirebot.domain.budget import PaidAction
    from hirebot.domain.entities import new_id

    async with container.uow_factory() as uow:
        await uow.paid_actions.add(
            PaidAction(
                id=new_id(),
                company_id=DEFAULT_COMPANY_ID,
                channel=Channel.HH.value,
                kind=PaidActionKind.RESUME_VIEW,
                cost=5.0,
                created_at=previous_month,
            )
        )
        await uow.commit()

    usage = await service.usage(DEFAULT_COMPANY_ID)

    assert usage.spent_today == 0.0
    assert usage.spent_month == 0.0, "прошлый месяц не входит в текущий лимит"


async def test_usage_counts_record_from_today(container: Any) -> None:
    """Запись, сделанная час назад, входит в дневной лимит."""
    service = BudgetService(container)
    hour_ago = service.now() - timedelta(hours=1)

    from hirebot.domain.budget import PaidAction
    from hirebot.domain.entities import new_id

    async with container.uow_factory() as uow:
        await uow.paid_actions.add(
            PaidAction(
                id=new_id(),
                company_id=DEFAULT_COMPANY_ID,
                channel=Channel.HH.value,
                kind=PaidActionKind.RESUME_VIEW,
                cost=5.0,
                created_at=hour_ago,
            )
        )
        await uow.commit()

    usage = await service.usage(DEFAULT_COMPANY_ID)

    assert usage.spent_today == pytest.approx(5.0)
    assert usage.spent_month == pytest.approx(5.0)


def test_default_costs_cover_all_kinds() -> None:
    for kind in PaidActionKind:
        assert kind.value in DEFAULT_COSTS, f"нет стоимости по умолчанию для {kind}"
