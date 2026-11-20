"""Бизнес-gauge'и: состояние воронки, снятое из БД при scrape.

Зачем это нужно.

Реестр метрик Prometheus живёт в процессе. Состояние диалогов меняет **воркер**,
а ``/metrics`` отдаёт **API** — разные процессы, поэтому «просто посчитать в
воркере» не получится: эти значения никто не увидит.

Два честных варианта: Pushgateway из воркера или снятие состояния при scrape.
Выбран второй: он не добавляет инфраструктуры, не требует сети между процессами
и всегда показывает актуальную картину на момент опроса. Цена — несколько
дешёвых запросов к БД на каждый scrape (обычно раз в 15–60 секунд).

Ошибки БД здесь гасятся намеренно: если база недоступна, ``/metrics`` не должен
падать — это ухудшило бы наблюдаемость ровно в тот момент, когда она нужнее
всего. Сама недоступность БД видна в ``/health/ready``.
"""

from __future__ import annotations

from hirebot.container import Container
from hirebot.domain.enums import ConversationState
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import observe_conversations

logger = get_logger(__name__)


async def refresh_business_gauges(container: Container) -> None:
    """Обновить gauge'и состояния воронки из БД.

    Считаем по каждому состоянию отдельно: так нет отдельного агрегирующего
    запроса, а количество диалогов ограничено здравым смыслом (пагинация
    защищает от случайного «взять всё»).
    """
    counts: dict[str, int] = {}
    try:
        async with container.uow_factory() as uow:
            for state in ConversationState:
                found = await uow.conversations.list_by_state([state], limit=1000)
                counts[state.value] = len(found)
    except Exception as exc:
        logger.warning("metrics_gauges_failed", error=type(exc).__name__)
        return

    observe_conversations(counts)


__all__ = ["refresh_business_gauges"]
