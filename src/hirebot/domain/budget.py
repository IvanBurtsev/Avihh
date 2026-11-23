"""Бюджет платных действий: дневной лимит, счётчик и жёсткий стоп.

Зачем это в домене, а не «где-то в адаптере».

Просмотр резюме и приглашение на hh.ru и Avito могут **списывать деньги**
(контакты, платные методы). Ошибка в одном месте цикла — и за сутки уходит
квота, которую никто не планировал. Поэтому правило «сколько можно потратить»
должно быть явным, проверяемым и одинаковым для всех вызовов.

Три принципа:

1. **Лимит проверяется до действия**, а не после. Списание нельзя «отменить».
2. **Неизвестное действие не считается бесплатным**: если стоимость не задана,
   берётся значение по умолчанию, а не ноль.
3. **Жёсткий стоп** (``hard_stop``) блокирует действие при исчерпании лимита.
   Мягкий режим только предупреждает — он нужен на пилоте, когда важнее
   посмотреть, чем остановиться.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum
from typing import Any


class PaidActionKind(StrEnum):
    """Виды действий, которые могут стоить денег.

    Значения совпадают с тем, что площадки называют по-разному, поэтому здесь
    единая терминология продукта:

    * ``RESUME_VIEW`` — открытие резюме или контактов;
    * ``INVITE`` — приглашение на собеседование;
    * ``MESSAGE`` — сообщение в чат (обычно бесплатно, но на Avito может
      входить в лимиты);
    * ``VACANCY_PUBLISH`` — публикация вакансии.
    """

    RESUME_VIEW = "resume_view"
    INVITE = "invite"
    MESSAGE = "message"
    VACANCY_PUBLISH = "vacancy_publish"


#: Стоимость по умолчанию в «единицах списаний».
#:
#: Единица — одно списание площадки: один контакт резюме, одно приглашение.
#: Ноль здесь опасен: неизвестное действие выглядело бы бесплатным.
DEFAULT_COSTS: dict[str, float] = {
    PaidActionKind.RESUME_VIEW.value: 1.0,
    PaidActionKind.INVITE.value: 1.0,
    PaidActionKind.MESSAGE.value: 0.0,
    PaidActionKind.VACANCY_PUBLISH.value: 1.0,
}


@dataclass(slots=True, frozen=True)
class BudgetLimits:
    """Настройки лимита.

    ``enabled=False`` полностью выключает проверку — это режим демо, где каналы
    не списывают деньги. В бою включать обязательно.
    """

    enabled: bool = True
    daily_limit: int = 100
    monthly_limit: int = 2000
    hard_stop: bool = True
    #: Стоимость по видам действий; дополняется значениями по умолчанию.
    costs: dict[str, float] = field(default_factory=lambda: dict(DEFAULT_COSTS))

    def __post_init__(self) -> None:
        if self.daily_limit < 0 or self.monthly_limit < 0:
            msg = "Лимиты бюджета не могут быть отрицательными"
            raise ValueError(msg)
        if self.monthly_limit and self.daily_limit > self.monthly_limit:
            msg = "Дневной лимит не может превышать месячный"
            raise ValueError(msg)

    def cost_of(self, kind: PaidActionKind) -> float:
        """Стоимость действия. Неизвестное — не бесплатное."""
        return float(self.costs.get(kind.value, DEFAULT_COSTS.get(kind.value, 1.0)))


@dataclass(slots=True, frozen=True)
class BudgetUsage:
    """Сколько потрачено и что из этого следует."""

    spent_today: float
    spent_month: float
    daily_limit: int
    monthly_limit: int

    @property
    def daily_left(self) -> float:
        return max(0.0, self.daily_limit - self.spent_today)

    @property
    def monthly_left(self) -> float:
        return max(0.0, self.monthly_limit - self.spent_month)

    @property
    def daily_exhausted(self) -> bool:
        return self.spent_today >= self.daily_limit

    @property
    def monthly_exhausted(self) -> bool:
        return self.spent_month >= self.monthly_limit

    def as_dict(self) -> dict[str, Any]:
        return {
            "spent_today": round(self.spent_today, 1),
            "spent_month": round(self.spent_month, 1),
            "daily_limit": self.daily_limit,
            "monthly_limit": self.monthly_limit,
            "daily_left": round(self.daily_left, 1),
            "monthly_left": round(self.monthly_left, 1),
        }


@dataclass(slots=True, frozen=True)
class BudgetDecision:
    """Решение по конкретному действию."""

    allowed: bool
    reason: str
    cost: float
    usage: BudgetUsage

    @property
    def blocked(self) -> bool:
        return not self.allowed


@dataclass(slots=True, frozen=True)
class PaidAction:
    """Запись журнала списаний: что, когда, сколько и по какому поводу."""

    id: Any
    company_id: Any
    channel: str
    kind: PaidActionKind
    cost: float
    reference: str = ""
    created_at: datetime | None = None

    def __post_init__(self) -> None:
        if self.cost < 0:
            msg = "Стоимость списания не может быть отрицательной"
            raise ValueError(msg)


def decide(
    *,
    limits: BudgetLimits,
    usage: BudgetUsage,
    kind: PaidActionKind,
) -> BudgetDecision:
    """Разрешить или запретить действие.

    Порядок проверок: выключенный бюджет → месячный лимит → дневной. Месячный
    первым, потому что он «дороже»: упереться в него посреди месяца хуже, чем
    в дневной.
    """
    cost = limits.cost_of(kind)

    if not limits.enabled:
        return BudgetDecision(
            allowed=True,
            reason="проверка бюджета выключена (демо-режим)",
            cost=cost,
            usage=usage,
        )

    if cost <= 0:
        return BudgetDecision(allowed=True, reason="действие бесплатное", cost=cost, usage=usage)

    if usage.spent_month >= limits.monthly_limit:
        return BudgetDecision(
            allowed=False,
            reason=(f"исчерпан месячный лимит: {usage.spent_month:.0f} из {limits.monthly_limit} списаний"),
            cost=cost,
            usage=usage,
        )

    if usage.spent_today + cost > limits.daily_limit:
        return BudgetDecision(
            allowed=False,
            reason=(
                f"исчерпан дневной лимит: {usage.spent_today:.0f} из "
                f"{limits.daily_limit} списаний (действие стоит {cost:.0f})"
            ),
            cost=cost,
            usage=usage,
        )

    return BudgetDecision(
        allowed=True,
        reason=(f"в пределах лимита: {usage.spent_today:.0f} из {limits.daily_limit} за сегодня"),
        cost=cost,
        usage=usage,
    )


__all__ = [
    "DEFAULT_COSTS",
    "BudgetDecision",
    "BudgetLimits",
    "BudgetUsage",
    "PaidAction",
    "PaidActionKind",
    "decide",
]
