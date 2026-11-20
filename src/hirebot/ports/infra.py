"""Инфраструктурные порты: HTTP, кэш, блокировки, лимиты, очередь, время.

Все внешние эффекты идут через эти протоколы. Благодаря этому:
* тесты не ходят в сеть и не поднимают Redis;
* ретраи, лимиты и кэш описаны **один раз** в реализации, а не в каждом адаптере;
* поведение воспроизводимо (``FakeClock`` вместо ``asyncio.sleep`` в тестах).
"""

from __future__ import annotations

from collections.abc import Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Protocol, runtime_checkable


@dataclass(slots=True, frozen=True)
class HttpResponse:
    """Нормализованный HTTP-ответ (транспорт не протекает в адаптеры)."""

    status_code: int
    body: dict[str, Any] | list[Any] | None = None
    text: str = ""
    headers: Mapping[str, str] = field(default_factory=dict)
    url: str = ""
    from_cache: bool = False

    @property
    def ok(self) -> bool:
        return 200 <= self.status_code < 300


@runtime_checkable
class HttpClientPort(Protocol):
    """HTTP-клиент с ретраями, лимитами и кэшем.

    Единая точка контроля исходящего трафика. Адаптеры площадок не создают
    собственные ``httpx.AsyncClient`` — иначе ретраи, таймауты и лимиты
    пришлось бы дублировать в каждом из них.
    """

    async def request(
        self,
        method: str,
        url: str,
        *,
        params: Mapping[str, Any] | None = None,
        json_body: Any | None = None,
        form: Mapping[str, Any] | None = None,
        headers: Mapping[str, str] | None = None,
        idempotent: bool = True,
        cache_ttl_s: float = 0.0,
        rate_limit_key: str | None = None,
        rate_limit_rps: float | None = None,
        bucket: str = "default",
    ) -> HttpResponse:
        """Выполнить запрос.

        ``idempotent=False`` отключает автоматические ретраи: повторять
        создание встречи или отправку сообщения вслепую нельзя.
        """
        ...

    async def aclose(self) -> None: ...


@runtime_checkable
class CachePort(Protocol):
    """Кэш с TTL. Реализации: Redis и in-process (fallback)."""

    async def get(self, key: str) -> bytes | None: ...
    async def set(self, key: str, value: bytes, *, ttl_s: float | None = None) -> None: ...
    async def delete(self, key: str) -> None: ...
    async def get_json(self, key: str) -> Any | None: ...
    async def set_json(self, key: str, value: Any, *, ttl_s: float | None = None) -> None: ...
    async def incr(self, key: str, *, ttl_s: float | None = None) -> int: ...


@runtime_checkable
class LockPort(Protocol):
    """Распределённая блокировка.

    Критично для hh.ru и Talantix: refresh-токен там **одноразовый**, и два
    параллельных обновления гарантированно ломают сессию. Блокировка делает
    обновление single-flight.
    """

    def acquire(
        self, key: str, *, ttl_s: float = 30.0, wait_s: float = 5.0
    ) -> AbstractAsyncContextManager[bool]: ...


@runtime_checkable
class RateLimiterPort(Protocol):
    """Token bucket. Защищает от 429 и от бана аккаунта."""

    async def acquire(self, key: str, *, rate_per_second: float, burst: int, wait: bool = True) -> float:
        """Дождаться разрешения. Возвращает, сколько секунд пришлось ждать."""
        ...


@runtime_checkable
class JobQueue(Protocol):
    """Очередь фоновых задач.

    ``dedupe_key`` обязателен в бизнес-сценариях: он гарантирует, что один и
    тот же шаг воронки не будет поставлен дважды при параллельном поллинге.
    """

    async def enqueue(
        self,
        job_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        delay_s: float = 0.0,
    ) -> str | None: ...

    async def start(self) -> None: ...
    async def close(self) -> None: ...


@runtime_checkable
class ClockPort(Protocol):
    """Источник времени и сна — вместо прямых ``datetime.now()``/``asyncio.sleep``."""

    def now(self) -> datetime: ...
    async def sleep(self, seconds: float) -> None: ...


@runtime_checkable
class UnitOfWorkFactory(Protocol):
    """Фабрика Unit of Work.

    Сервисы получают её, а не сессию БД: так один и тот же сервис работает
    и в API, и в воркере, и в тесте с SQLite в памяти.
    """

    def __call__(self) -> AbstractAsyncContextManager[Any]: ...


@runtime_checkable
class AsyncClosable(Protocol):
    """Ресурс, который нужно корректно закрыть при остановке приложения."""

    async def aclose(self) -> None: ...


class NullCache:
    """Реализация-заглушка: кэш выключен, поведение при этом корректно."""

    async def get(self, key: str) -> bytes | None:
        return None

    async def set(self, key: str, value: bytes, *, ttl_s: float | None = None) -> None:
        return None

    async def delete(self, key: str) -> None:
        return None

    async def get_json(self, key: str) -> Any | None:
        return None

    async def set_json(self, key: str, value: Any, *, ttl_s: float | None = None) -> None:
        return None

    async def incr(self, key: str, *, ttl_s: float | None = None) -> int:
        return 1
