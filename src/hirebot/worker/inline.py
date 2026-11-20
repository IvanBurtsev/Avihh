"""Inline-режим воркера: работа без Redis и arq.

Демо и тесты запускаются одной командой (``hirebot worker --inline``). В этом
режиме ``JobQueue`` — это ``InlineJobQueue``: задача выполняется в текущем
процессе сразу при постановке, а цикл ниже добирает то, что осталось в
транзакционном outbox.

Смысл разделения: контракт задач один и тот же, меняется только брокер.
"""

from __future__ import annotations

from typing import Any

from hirebot.container import Container
from hirebot.logging_setup import get_logger
from hirebot.worker import services

logger = get_logger(__name__)

#: Пауза между проходами диспетчера по умолчанию.
DEFAULT_INLINE_INTERVAL_S = 5.0


async def run_inline_worker(
    container: Container,
    *,
    interval_s: float = DEFAULT_INLINE_INTERVAL_S,
    batch_size: int = 20,
    max_iterations: int | None = None,
) -> int:
    """Крутить цикл доставки outbox без брокера.

    Возвращает суммарное количество доставленных записей — это удобно и для
    лога, и для тестов.

    ``max_iterations`` ограничивает число проходов (нужно тестам и smoke-прогонам);
    ``None`` означает «крутить до остановки процесса».
    """
    dispatcher = services.outbox_dispatcher(container)
    delivered = 0
    iterations = 0

    logger.info("inline_worker_started", interval_s=interval_s, batch_size=batch_size)
    while max_iterations is None or iterations < max_iterations:
        iterations += 1
        try:
            processed = await dispatcher.drain(batch_size=batch_size)
        except Exception as exc:
            # Падение одного прохода не должно убивать воркер: чаще всего это
            # временная недоступность площадки, а записи остаются в outbox.
            logger.error("inline_drain_failed", error_type=type(exc).__name__, error=str(exc))
        else:
            count = int(processed or 0)
            delivered += count
            if count:
                logger.info("inline_drain", processed=count, delivered_total=delivered)

        if max_iterations is not None and iterations >= max_iterations:
            break
        await container.clock.sleep(interval_s)

    logger.info("inline_worker_stopped", iterations=iterations, delivered=delivered)
    return delivered


async def run_inline_once(container: Container, *, batch_size: int = 20) -> int:
    """Один проход диспетчера — используется в ``hirebot worker --inline --once``."""
    return await run_inline_worker(container, batch_size=batch_size, max_iterations=1)


def inline_summary(delivered: int) -> dict[str, Any]:
    """Итог inline-прогона в виде, пригодном для печати в CLI."""
    return {"status": "stopped", "delivered": delivered}


__all__ = [
    "DEFAULT_INLINE_INTERVAL_S",
    "inline_summary",
    "run_inline_once",
    "run_inline_worker",
]
