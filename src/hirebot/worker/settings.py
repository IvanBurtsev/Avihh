"""Настройки arq-воркера: очередь, cron-расписание, жизненный цикл.

Контейнер собирается в ``on_startup`` и закрывается в ``on_shutdown``: одна
сборка на процесс воркера, а не на задачу. Это важно, потому что контейнер
держит пул соединений БД, HTTP-клиент и очередь.

Расписание:

* поллинг площадок — раз в 60 секунд (``second=0``);
* доставка outbox — каждые 10 секунд (``second={0,10,20,30,40,50}``), плюс
  сразу при старте воркера: после рестарта накопленные сообщения должны уйти
  немедленно, а не через десять секунд.
"""

from __future__ import annotations

from typing import Any, ClassVar

from arq import cron
from arq.connections import RedisSettings

from hirebot.config import get_settings
from hirebot.container import Container, build_container
from hirebot.logging_setup import configure_logging, get_logger
from hirebot.worker.tasks import (
    book_interview,
    dispatch_outbox,
    poll_channel,
    poll_enabled_channels,
    process_conversation,
)

logger = get_logger(__name__)

#: Настройки читаются один раз на процесс воркера (arq CLI импортирует модуль).
settings = get_settings()


async def startup(ctx: dict[str, Any]) -> None:
    """Собрать контейнер и поднять очередь."""
    current = get_settings()
    configure_logging(level=current.log_level, json_output=current.log_json)
    container = await build_container(current)
    await container.queue.start()
    ctx["container"] = container
    logger.info(
        "worker_startup",
        env=current.env,
        channels=[channel.value for channel in container.enabled_channels],
        queue=current.queue.backend,
    )


async def shutdown(ctx: dict[str, Any]) -> None:
    """Закрыть ресурсы контейнера."""
    container = ctx.pop("container", None)
    if isinstance(container, Container):
        await container.aclose()
    logger.info("worker_shutdown")


class WorkerSettings:
    """Класс, который ищет arq CLI (``arq hirebot.worker.settings.WorkerSettings``)."""

    redis_settings: ClassVar[RedisSettings] = RedisSettings.from_dsn(settings.redis.dsn)

    functions: ClassVar[list[Any]] = [
        poll_channel,
        process_conversation,
        book_interview,
        dispatch_outbox,
        poll_enabled_channels,
    ]

    cron_jobs: ClassVar[list[Any]] = [
        # Поллинг: раз в минуту, в начале минуты.
        cron(poll_enabled_channels, name="poll_channels", second=0),
        # Outbox: каждые 10 секунд и сразу после старта.
        cron(
            dispatch_outbox,
            name="dispatch_outbox",
            second={0, 10, 20, 30, 40, 50},
            run_at_startup=True,
        ),
    ]

    on_startup: ClassVar[Any] = startup
    on_shutdown: ClassVar[Any] = shutdown

    queue_name: ClassVar[str] = settings.queue.queue_name
    job_timeout: ClassVar[int] = settings.queue.job_timeout_s
    max_tries: ClassVar[int] = settings.queue.max_attempts
    # Результат храним час: его читает административный API при разборе инцидентов.
    keep_result: ClassVar[int] = 3600


__all__ = ["WorkerSettings", "settings", "shutdown", "startup"]
