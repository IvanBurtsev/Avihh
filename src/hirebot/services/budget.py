"""Контроль расходов на платные действия площадок.

Просмотр резюме и приглашение на hh.ru и Avito могут списывать деньги. Этот
сервис отвечает на два вопроса:

1. **Можно ли сейчас?** — сравнение с дневным и месячным лимитом до действия.
2. **Сколько уже ушло?** — журнал и сумма за сегодня/месяц.

Ключевое правило: проверка идёт **до** вызова площадки, а списание записывается
**после** успешного вызова. Если площадка отказала, расход не записывается.

Демо-каналы (fake) не списывают: иначе демо показывало бы расходы, которых нет.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from hirebot.domain.budget import (
    BudgetDecision,
    BudgetLimits,
    BudgetUsage,
    PaidAction,
    PaidActionKind,
    decide,
)
from hirebot.domain.entities import new_id
from hirebot.domain.enums import Channel
from hirebot.domain.errors import BudgetExceeded, DomainError
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_paid_action
from hirebot.services.base import ServiceBase

logger = get_logger(__name__)

#: Каналы, действия в которых могут стоить денег. Демо-адаптеры сюда не входят.
CHARGEABLE_CHANNELS: frozenset[Channel] = frozenset({Channel.HH, Channel.AVITO})


class BudgetService(ServiceBase):
    """Лимиты, проверка и журнал платных действий."""

    def limits(self) -> BudgetLimits:
        settings = self.settings.budget
        return BudgetLimits(
            enabled=settings.enabled,
            daily_limit=settings.daily_limit,
            monthly_limit=settings.monthly_limit,
            hard_stop=settings.hard_stop,
        )

    def is_chargeable(self, channel: Channel) -> bool:
        """Списывает ли канал деньги за действия.

        Демо-адаптеры (``is_fake``) не списывают: в тестах и локальной проверке
        площадка не вызывается вовсе. Иначе бюджет блокировал бы работу там,
        где расходов нет.
        """
        if channel not in CHARGEABLE_CHANNELS:
            return False
        try:
            adapter = self._container.channel(channel)
        except DomainError:
            return False
        return not bool(getattr(adapter, "is_fake", False))

    async def usage(self, company_id: UUID) -> BudgetUsage:
        """Потрачено за сегодня и за текущий месяц."""
        limits = self.limits()
        now = self.now()
        day_start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        month_start = day_start.replace(day=1)
        async with self.uow() as uow:
            today = await uow.paid_actions.spent_since(company_id, day_start)
            month = await uow.paid_actions.spent_since(company_id, month_start)
        return BudgetUsage(
            spent_today=today,
            spent_month=month,
            daily_limit=limits.daily_limit,
            monthly_limit=limits.monthly_limit,
        )

    async def check(
        self,
        *,
        company_id: UUID,
        channel: Channel,
        kind: PaidActionKind,
    ) -> BudgetDecision:
        """Можно ли выполнить действие, не превысив лимит."""
        usage = await self.usage(company_id)
        decision = decide(limits=self.limits(), usage=usage, kind=kind)
        if not decision.allowed:
            logger.warning(
                "budget_blocked",
                company_id=str(company_id),
                channel=channel.value,
                kind=kind.value,
                reason=decision.reason,
            )
        return decision

    async def ensure_allowed(
        self,
        *,
        company_id: UUID,
        channel: Channel,
        kind: PaidActionKind,
    ) -> BudgetDecision:
        """Проверить и, при жёстком стопе, запретить действие.

        Для демо-каналов и бесплатных действий проверка не блокирует: иначе
        локальная проверка упиралась бы в лимиты, которых нет.
        """
        if not self.is_chargeable(channel):
            usage = await self.usage(company_id)
            return BudgetDecision(
                allowed=True,
                reason=f"канал {channel.value!r} не списывает деньги",
                cost=0.0,
                usage=usage,
            )
        decision = await self.check(company_id=company_id, channel=channel, kind=kind)
        limits = self.limits()
        if decision.blocked and limits.enabled and limits.hard_stop:
            raise BudgetExceeded(
                f"Действие «{kind.value}» заблокировано: {decision.reason}. "
                "Повысьте лимит или дождитесь следующего периода."
            )
        return decision

    async def record(
        self,
        *,
        company_id: UUID,
        channel: Channel,
        kind: PaidActionKind,
        reference: str = "",
        cost: float | None = None,
    ) -> PaidAction:
        """Записать списание после успешного вызова площадки."""
        amount = self.limits().cost_of(kind) if cost is None else cost
        action = PaidAction(
            id=new_id(),
            company_id=company_id,
            channel=channel.value,
            kind=kind,
            cost=amount,
            reference=reference,
            created_at=self.now(),
        )
        async with self.uow() as uow:
            saved = await uow.paid_actions.add(action)
            await uow.commit()
        record_paid_action(kind.value, channel.value, amount)
        logger.info(
            "paid_action_recorded",
            company_id=str(company_id),
            channel=channel.value,
            kind=kind.value,
            cost=amount,
            reference=reference,
        )
        return saved

    def period_bounds(self) -> tuple[datetime, datetime]:
        """Границы текущего дня (для отчётов и UI)."""
        now = self.now()
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, start + timedelta(days=1)


def utc_midnight(moment: datetime) -> datetime:
    """Начало суток в UTC — вынесено для тестов и отчётов."""
    aware = moment if moment.tzinfo is not None else moment.replace(tzinfo=UTC)
    return aware.astimezone(UTC).replace(hour=0, minute=0, second=0, microsecond=0)


__all__ = ["CHARGEABLE_CHANNELS", "BudgetService", "utc_midnight"]
