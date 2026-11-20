"""Слой адаптеров: сборка реализаций портов.

Единственное место, где известно, какой класс реализует ``ChannelPort``,
``CalendarPort`` и ``LlmPort``. ``container.py`` вызывает только три функции
отсюда и не знает ни одного имени конкретного адаптера.

Правила, которые соблюдают все фабрики:

* ничего не создаём «на всякий случай»: выключенный канал/календарь/провайдер
  не поднимает ни соединений, ни фоновых задач;
* ошибка конфигурации — это ``ConfigError``/``ChannelNotConfigured`` с текстом,
  объясняющим, какую переменную окружения задать;
* ``locks`` — необязательный параметр. Он не входит в замороженную сигнатуру,
  но добавлен как keyword-only с значением по умолчанию: ``container.py``
  вызывает фабрики без него, а тесты и будущий код могут передать
  инфраструктурный (Redis) ``LockPort``, чтобы single-flight обновление токенов
  работало между воркерами, а не только внутри процесса.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

from hirebot.domain.enums import Channel
from hirebot.domain.errors import ChannelNotConfigured, ConfigError
from hirebot.logging_setup import get_logger
from hirebot.ports.channels import CalendarPort, ChannelPort
from hirebot.ports.infra import CachePort, ClockPort, HttpClientPort, LockPort

if TYPE_CHECKING:
    from hirebot.config import Settings
    from hirebot.ports.llm import LlmPort

__all__ = ["build_calendar", "build_channels", "build_llm"]

logger = get_logger(__name__)


def build_channels(
    settings: Settings,
    *,
    http: HttpClientPort,
    cipher: object | None,
    cache: CachePort,
    clock: ClockPort,
    locks: LockPort | None = None,
) -> Mapping[Channel, ChannelPort]:
    """Собрать адаптеры каналов по настройкам.

    Асимметрия осознанная:

    * **hh.ru** попадает в словарь только при ``HIREBOT_HH__ENABLED=true``.
      Иначе ``container.channel(Channel.HH)`` честно скажет, что канал не
      сконфигурирован, а не будет молча ронять поллинг;
    * **Avito** присутствует всегда: его контракт ``enabled=False`` — «чтение
      отдаёт пустые списки, запись бросает ``ChannelNotConfigured``». Именно
      благодаря этому пайплайн живёт, пока доступ к Avito API не подтверждён.
    """
    channels: dict[Channel, ChannelPort] = {}

    if settings.hh.enabled:
        from hirebot.adapters.channels.hh.adapter import HhChannelAdapter

        channels[Channel.HH] = HhChannelAdapter(
            settings.hh,
            http=http,
            clock=clock,
            cache=cache,
            cipher=cipher,
            locks=locks,
        )
    else:
        logger.info("channel_disabled", channel=Channel.HH.value)

    from hirebot.adapters.channels.avito.adapter import AvitoChannelAdapter

    channels[Channel.AVITO] = AvitoChannelAdapter(
        settings.avito,
        http=http,
        clock=clock,
        cache=cache,
        cipher=cipher,
    )
    if not settings.avito.enabled:
        logger.info("channel_disabled", channel=Channel.AVITO.value)
    return channels


def build_calendar(
    settings: Settings,
    *,
    http: HttpClientPort,
    clock: ClockPort,
    cache: CachePort | None = None,
    cipher: object | None = None,
    locks: LockPort | None = None,
) -> CalendarPort | None:
    """Собрать календарь Talantix (``None``, если он выключен)."""
    if not settings.talantix.enabled:
        logger.info("calendar_disabled", provider="talantix")
        return None
    if settings.talantix.access_token is None:
        msg = (
            "Календарь Talantix включён, но не задан HIREBOT_TALANTIX__ACCESS_TOKEN. "
            "Токены выдаёт администратор Talantix в личном кабинете (тариф «Оптимальный»)"
        )
        raise ChannelNotConfigured(msg)

    from hirebot.adapters.calendar.talantix.adapter import TalantixCalendarAdapter

    return TalantixCalendarAdapter(
        settings.talantix,
        http=http,
        clock=clock,
        cache=cache,
        cipher=cipher,
        locks=locks,
    )


def build_llm(settings: Settings, *, http: HttpClientPort, cache: CachePort) -> LlmPort:
    """Собрать LLM-провайдер.

    ``fake`` — детерминированная реализация без сети: демо и тесты работают без
    ключей. ``openai_compat`` — любой OpenAI-совместимый эндпоинт.
    """
    if settings.llm.provider == "fake":
        from hirebot.adapters.llm.fake import FakeLlmAdapter

        logger.info("llm_provider_selected", provider="fake")
        return FakeLlmAdapter()

    if settings.llm.api_key is None:
        msg = (
            "HIREBOT_LLM__API_KEY не задан для провайдера "
            f"{settings.llm.provider!r}: вызовы модели невозможны"
        )
        raise ConfigError(msg)

    from hirebot.adapters.llm.openai_compat import OpenAiCompatAdapter

    logger.info(
        "llm_provider_selected",
        provider=settings.llm.provider,
        model=settings.llm.model,
        send_pii=settings.llm.send_pii,
    )
    return OpenAiCompatAdapter(settings.llm, http=http, cache=cache)
