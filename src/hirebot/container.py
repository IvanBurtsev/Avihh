"""DI-контейнер и сборочный контракт.

Единственное место, где конкретные реализации связываются с портами.
Сервисы получают готовый ``Container`` и не знают, Redis у нас или память,
HH или fake-адаптер.

Всё строится лениво: инфраструктура, которая не нужна (например, Redis при
``QUEUE__BACKEND=inline``), не поднимается вовсе.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from typing import Any

from hirebot.config import Settings, get_settings
from hirebot.domain.enums import Channel
from hirebot.domain.errors import ChannelNotConfigured
from hirebot.logging_setup import get_logger
from hirebot.ports.channels import CalendarPort, ChannelPort
from hirebot.ports.infra import (
    CachePort,
    ClockPort,
    HttpClientPort,
    JobQueue,
    LockPort,
    RateLimiterPort,
)
from hirebot.ports.llm import LlmPort
from hirebot.ports.repositories import UnitOfWork

logger = get_logger(__name__)

#: Фабрика Unit of Work: каждый вызов даёт новую транзакционную границу.
UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


@dataclass(slots=True)
class Container:
    """Собранное приложение.

    Передаётся в сервисы, в API-роутеры и в задачи воркера. В тестах
    собирается вручную из fake-реализаций — без БД, Redis и сети.
    """

    settings: Settings
    uow_factory: UowFactory
    http: HttpClientPort
    cache: CachePort
    locks: LockPort
    rate_limiter: RateLimiterPort
    queue: JobQueue
    clock: ClockPort
    llm: LlmPort
    cipher: Any  # FieldCipher: шифрование ПДн
    channels: Mapping[Channel, ChannelPort] = field(default_factory=dict)
    calendar: CalendarPort | None = None

    def channel(self, name: Channel) -> ChannelPort:
        """Получить адаптер канала.

        Явная ошибка лучше ``KeyError``: если канал выключен в конфиге, это
        нормальная ситуация, и сообщение должно объяснять причину. Ошибка
        доменная — её ловит обработчик API и отдаёт понятный ответ вместо 500.
        """
        try:
            return self.channels[name]
        except KeyError as exc:
            msg = (
                f"Канал {name.value!r} не сконфигурирован. "
                f"Включите его в .env (HIREBOT_{name.value.upper()}__ENABLED=true) "
                f"и задайте учётные данные."
            )
            raise ChannelNotConfigured(msg) from exc

    def require_calendar(self) -> CalendarPort:
        if self.calendar is None:
            msg = (
                "Календарь не сконфигурирован: задайте HIREBOT_TALANTIX__ENABLED=true "
                "и HIREBOT_TALANTIX__ACCESS_TOKEN."
            )
            raise RuntimeError(msg)
        return self.calendar

    @property
    def enabled_channels(self) -> tuple[Channel, ...]:
        return tuple(self.channels)

    async def aclose(self) -> None:
        """Корректно закрыть ресурсы (порядок важен: сначала очередь, потом HTTP)."""
        for resource in (self.queue, self.http):
            close = getattr(resource, "aclose", None)
            if close is not None:
                try:
                    await close()
                except Exception:
                    logger.warning("close_failed", resource=type(resource).__name__)


async def build_container(settings: Settings | None = None) -> Container:
    """Собрать контейнер по настройкам.

    Импорты намеренно внутри функции: модуль остаётся импортируемым даже если
    часть адаптеров не установлена или выключена.
    """
    settings = settings or get_settings()

    from hirebot.adapters import build_calendar, build_channels, build_llm
    from hirebot.infra.cache import create_cache
    from hirebot.infra.clock import SystemClock
    from hirebot.infra.db.session import create_uow_factory
    from hirebot.infra.http import create_http_client
    from hirebot.infra.locks import create_lock
    from hirebot.infra.queue import create_queue
    from hirebot.infra.ratelimit import create_rate_limiter
    from hirebot.infra.security.crypto import FieldCipher

    clock = SystemClock()
    cache = await create_cache(settings)
    locks = await create_lock(settings)
    rate_limiter = await create_rate_limiter(settings, cache)
    cipher = FieldCipher.from_settings(settings)
    http = create_http_client(settings, cache=cache, rate_limiter=rate_limiter, locks=locks, clock=clock)
    queue = await create_queue(settings)
    uow_factory: UowFactory = create_uow_factory(settings)

    channels = build_channels(settings, http=http, cipher=cipher, cache=cache, clock=clock)
    calendar = build_calendar(settings, http=http, clock=clock)
    llm = build_llm(settings, http=http, cache=cache)

    logger.info(
        "container_ready",
        env=settings.env,
        channels=[c.value for c in channels],
        calendar=calendar is not None,
        llm=settings.llm.provider,
        queue=settings.queue.backend,
        postgres=settings.database.is_postgres,
    )

    # Загружаем адаптеры каналов заранее, чтобы ошибка конфигурации всплыла при старте,
    # а не в момент, когда кандидат уже ждёт ответа.
    for name, adapter in channels.items():
        try:
            await adapter.healthcheck()
        except Exception as exc:
            logger.warning("channel_healthcheck_failed", channel=name.value, error=str(exc))

    return Container(
        settings=settings,
        uow_factory=uow_factory,
        http=http,
        cache=cache,
        locks=locks,
        rate_limiter=rate_limiter,
        queue=queue,
        clock=clock,
        llm=llm,
        cipher=cipher,
        channels=channels,
        calendar=calendar,
    )


def build_fake_container(settings: Settings | None = None, *, seed_demo: bool = True) -> Container:
    """Контейнер на fake-реализациях: без БД, Redis и сети.

    Используется в unit-тестах и в ``hirebot cli demo``. ``seed_demo=False``
    даёт пустой контейнер — это нужно тестам, которые наполняют его сами.
    """
    from hirebot.testing.fakes import build_fake_container as _build

    return _build(settings, seed_demo=seed_demo)


__all__ = ["Container", "UowFactory", "build_container", "build_fake_container"]
