"""Распределённые блокировки.

Блокировка здесь — не «красота», а способ не потерять OAuth-сессию. У hh.ru и
Talantix refresh-токен **одноразовый**: два параллельных воркера, обновивших
токен одновременно, гарантированно ломают сессию («token has already been
refreshed»). ``LockPort`` делает обновление single-flight.

Требования к реализации:
* освобождение только владельцем (иначе чужой лок можно снять по таймауту);
* у лока есть TTL: упавший воркер не должен блокировать систему навсегда;
* ``acquire`` возвращает ``bool`` внутри ``async with``, а не бросает исключение:
  «не получил лок» — штатная ситуация, вызывающая сторона решает, что делать.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from redis.asyncio import Redis

from hirebot.config import Settings
from hirebot.domain.errors import ConfigError
from hirebot.infra.redis_client import close_redis_client, create_redis_client
from hirebot.logging_setup import get_logger
from hirebot.ports.infra import LockPort

logger = get_logger(__name__)

#: Атомарное «снять лок, только если он мой».
#: Без сравнения токена возможна классическая гонка: воркер A держит лок дольше
#: TTL, лок достаётся B, затем A по ``finally`` удаляет **чужой** лок.
RELEASE_SCRIPT = """
if redis.call('GET', KEYS[1]) == ARGV[1] then
    return redis.call('DEL', KEYS[1])
end
return 0
"""

#: Минимальная пауза между попытками взять лок (секунды).
_MIN_RETRY_DELAY_S = 0.02
#: Максимальная пауза между попытками (секунды).
_MAX_RETRY_DELAY_S = 0.1


class RedisLock:
    """Распределённый лок на Redis: ``SET NX PX`` + освобождение по токену (Lua)."""

    __slots__ = ("_client", "_prefix", "_release")

    def __init__(self, client: Redis, *, prefix: str = "hirebot:lock:") -> None:
        self._client = client
        self._prefix = prefix
        self._release = client.register_script(RELEASE_SCRIPT)

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    @asynccontextmanager
    async def acquire(self, key: str, *, ttl_s: float = 30.0, wait_s: float = 5.0) -> AsyncIterator[bool]:
        """Взять лок. Внутри ``async with`` доступен ``bool`` — взяли или нет."""
        if ttl_s <= 0:
            msg = "RedisLock: ttl_s должен быть > 0, иначе лок не защищает"
            raise ValueError(msg)
        redis_key = self._key(key)
        token = uuid.uuid4().hex
        acquired = await self._try_acquire(redis_key, token, ttl_s, wait_s)
        if not acquired:
            logger.warning("lock_timeout", key=key, wait_s=wait_s)
        try:
            yield acquired
        finally:
            if acquired:
                try:
                    released = await self._release(keys=[redis_key], args=[token])
                except Exception as exc:
                    logger.warning("lock_release_failed", key=key, reason=type(exc).__name__)
                else:
                    if not released:
                        logger.warning("lock_release_lost", key=key, note="лок истёк по TTL")

    async def _try_acquire(self, redis_key: str, token: str, ttl_s: float, wait_s: float) -> bool:
        deadline = time.monotonic() + max(0.0, wait_s)
        delay = _MIN_RETRY_DELAY_S
        while True:
            ok = await self._client.set(redis_key, token, nx=True, px=max(1, int(ttl_s * 1000)))
            if ok:
                return True
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(delay, remaining))
            delay = min(delay * 2, _MAX_RETRY_DELAY_S)


class MemoryLock:
    """Лок в памяти процесса: ``asyncio.Lock`` на ключ и честное ожидание с таймаутом.

    Используется, когда Redis недоступен (демо, тесты, один процесс). Ограничение
    очевидно: между процессами он не работает — при ``QUEUE__BACKEND=arq`` нужен
    ``RedisLock``, иначе single-flight refresh не защищён.
    """

    __slots__ = ("_locks",)

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        # dict.setdefault в одном потоке event loop атомарен: гонки за создание
        # двух разных Lock-ов на один ключ не будет.
        return self._locks.setdefault(key, asyncio.Lock())

    @asynccontextmanager
    async def acquire(self, key: str, *, ttl_s: float = 30.0, wait_s: float = 5.0) -> AsyncIterator[bool]:
        """Взять лок, подождав не дольше ``wait_s``.

        ``ttl_s`` здесь не используется: лок живёт, пока жив его владелец.
        Параметр оставлен ради совместимости с ``LockPort``.
        """
        del ttl_s
        lock = self._lock_for(key)
        acquired = False
        if wait_s <= 0:
            if not lock.locked():
                await lock.acquire()
                acquired = True
        else:
            try:
                await asyncio.wait_for(lock.acquire(), timeout=wait_s)
            except TimeoutError:
                acquired = False
            else:
                acquired = True
        if not acquired:
            logger.warning("lock_timeout", key=key, wait_s=wait_s)
        try:
            yield acquired
        finally:
            if acquired and lock.locked():
                lock.release()


async def create_lock(settings: Settings) -> LockPort:
    """Собрать блокировки: Redis при доступности, иначе — in-process.

    Для локов (в отличие от кэша) потеря Redis недопустима, если воркеров
    несколько. Поэтому при ``required=true`` ошибка подключения пробрасывается:
    работать «наполовину защищённым» хуже, чем не стартовать.
    """
    client = create_redis_client(settings)
    try:
        await asyncio.wait_for(client.ping(), timeout=1.5)
    except Exception as exc:
        await close_redis_client(client)
        if settings.redis.required:
            msg = "Redis недоступен, а HIREBOT_REDIS__REQUIRED=true: блокировки обязательны"
            raise ConfigError(msg) from exc
        logger.warning("lock_fallback_to_memory", reason=type(exc).__name__)
        return MemoryLock()
    logger.info("lock_redis_ready")
    return RedisLock(client)


__all__ = ["RELEASE_SCRIPT", "MemoryLock", "RedisLock", "create_lock"]
