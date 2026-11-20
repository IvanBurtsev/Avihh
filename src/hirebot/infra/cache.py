"""Кэш: Redis с деградацией до in-process реализации.

Кэш не является источником истины. Его задачи: не платить дважды за один вызов
LLM, не дёргать справочники площадок каждый цикл и не обрабатывать один и тот
же отклик повторно. Поэтому при недоступном Redis система **продолжает
работать** — просто с ``MemoryCache`` (см. ``docs/ARCHITECTURE.md``, п. 2.6).

Сериализация — ``orjson``: он быстрее стандартного ``json`` и уже есть в стеке.
"""

from __future__ import annotations

import asyncio
import math
from collections import OrderedDict
from typing import Any, cast

import orjson
from redis.asyncio import Redis

from hirebot.config import Settings
from hirebot.domain.errors import ConfigError
from hirebot.infra.clock import SystemClock
from hirebot.infra.redis_client import close_redis_client, create_redis_client
from hirebot.logging_setup import get_logger
from hirebot.ports.infra import CachePort, ClockPort

logger = get_logger(__name__)

#: Сколько раз ждать Redis при старте (``create_cache``) прежде чем деградировать.
CONNECT_PROBE_TIMEOUT_S = 1.5


class MemoryCache:
    """Кэш в памяти процесса с TTL и LRU-ограничением по числу ключей.

    Зачем LRU: без ограничения кэш — это утечка памяти. Поллер работает сутками,
    ключи ``seen:{channel}:{response_id}`` не повторяются, и через неделю процесс
    съел бы всю память.

    Зачем ``asyncio.Lock``: между чтением и записью в ``OrderedDict`` есть
    ``await``-точки в вызывающем коде, но внутри самого кэша операции должны быть
    атомарными относительно друг друга (get + move_to_end, TTL + evict).
    """

    __slots__ = ("_clock", "_data", "_hits", "_lock", "_max_keys", "_misses")

    def __init__(self, *, max_keys: int = 1024, clock: ClockPort | None = None) -> None:
        if max_keys < 1:
            msg = "MemoryCache: max_keys должен быть >= 1"
            raise ValueError(msg)
        self._data: OrderedDict[str, tuple[bytes, float | None]] = OrderedDict()
        self._lock = asyncio.Lock()
        self._max_keys = max_keys
        self._clock: ClockPort = clock if clock is not None else SystemClock()
        self._hits = 0
        self._misses = 0

    @property
    def size(self) -> int:
        """Текущее число ключей (с учётом ещё не вычищенных просроченных)."""
        return len(self._data)

    @property
    def hits(self) -> int:
        return self._hits

    @property
    def misses(self) -> int:
        return self._misses

    def _now(self) -> float:
        return self._clock.now().timestamp()

    async def get(self, key: str) -> bytes | None:
        async with self._lock:
            item = self._data.get(key)
            if item is None:
                self._misses += 1
                return None
            value, expires_at = item
            if expires_at is not None and expires_at <= self._now():
                del self._data[key]
                self._misses += 1
                return None
            self._data.move_to_end(key)
            self._hits += 1
            return value

    async def set(self, key: str, value: bytes, *, ttl_s: float | None = None) -> None:
        async with self._lock:
            self._store(key, value, ttl_s)

    async def delete(self, key: str) -> None:
        async with self._lock:
            self._data.pop(key, None)

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        return orjson.loads(raw)

    async def set_json(self, key: str, value: Any, *, ttl_s: float | None = None) -> None:
        await self.set(key, orjson.dumps(value), ttl_s=ttl_s)

    async def incr(self, key: str, *, ttl_s: float | None = None) -> int:
        """Атомарно увеличить счётчик (счётчик просмотров резюме, квоты и т.п.)."""
        async with self._lock:
            now = self._now()
            item = self._data.get(key)
            if item is not None:
                raw, expires_at = item
                if expires_at is None or expires_at > now:
                    count = int(raw.decode("ascii")) + 1
                    self._data[key] = (str(count).encode("ascii"), expires_at)
                    self._data.move_to_end(key)
                    return count
            self._store(key, b"1", ttl_s)
            return 1

    def _store(self, key: str, value: bytes, ttl_s: float | None) -> None:
        """Положить значение (вызывается только под ``self._lock``)."""
        if ttl_s is not None and ttl_s <= 0:
            # TTL <= 0 означает «не кэшировать»: иначе запись существовала бы
            # ровно до следующего чтения и только путала бы семантику.
            return
        expires = self._now() + ttl_s if ttl_s is not None else None
        self._data[key] = (value, expires)
        self._data.move_to_end(key)
        while len(self._data) > self._max_keys:
            self._evict_one()

    def _evict_one(self) -> None:
        """Вытеснить самый старый ключ, попутно выбросив просроченные."""
        now = self._now()
        for stale_key, (_, expires_at) in list(self._data.items()):
            if expires_at is not None and expires_at <= now:
                del self._data[stale_key]
                return
        self._data.popitem(last=False)

    def clear(self) -> None:
        self._data.clear()


class RedisCache:
    """Кэш в Redis.

    Ключи префиксуются, чтобы hirebot не конфликтовал с другими приложениями на
    том же инстансе Redis. ``ttl_s=None`` — бессрочно (используется редко),
    ``ttl_s <= 0`` — не кэшировать вовсе.
    """

    __slots__ = ("_client", "_prefix")

    def __init__(self, client: Redis, *, prefix: str = "hirebot:cache:") -> None:
        self._client = client
        self._prefix = prefix

    @property
    def client(self) -> Redis:
        """Клиент нужен фабрике rate limiter (общий пул соединений)."""
        return self._client

    def _key(self, key: str) -> str:
        return f"{self._prefix}{key}"

    async def get(self, key: str) -> bytes | None:
        raw = await self._client.get(self._key(key))
        return cast("bytes | None", raw)

    async def set(self, key: str, value: bytes, *, ttl_s: float | None = None) -> None:
        if ttl_s is not None and ttl_s <= 0:
            return
        if ttl_s is None:
            await self._client.set(self._key(key), value)
        else:
            await self._client.set(self._key(key), value, px=max(1, int(ttl_s * 1000)))

    async def delete(self, key: str) -> None:
        await self._client.delete(self._key(key))

    async def get_json(self, key: str) -> Any | None:
        raw = await self.get(key)
        if raw is None:
            return None
        return orjson.loads(raw)

    async def set_json(self, key: str, value: Any, *, ttl_s: float | None = None) -> None:
        await self.set(key, orjson.dumps(value), ttl_s=ttl_s)

    async def incr(self, key: str, *, ttl_s: float | None = None) -> int:
        full_key = self._key(key)
        count = int(await self._client.incr(full_key))
        if count == 1 and ttl_s is not None and ttl_s > 0:
            # TTL ставим только на первом инкременте и только если его ещё нет:
            # ``nx=True`` не даёт сбросить окно у существующего счётчика.
            await self._client.expire(full_key, max(1, math.ceil(ttl_s)), nx=True)
        return count

    async def ping(self) -> bool:
        return bool(await self._client.ping())

    async def aclose(self) -> None:
        await close_redis_client(self._client)


async def create_cache(settings: Settings) -> CachePort:
    """Собрать кэш: Redis, а при его недоступности — ``MemoryCache``.

    Падать из-за кэша нельзя: он не источник истины. Исключение — явное
    ``HIREBOT_REDIS__REQUIRED=true``: тогда недоступность Redis — ошибка
    конфигурации и должна быть видна сразу.
    """
    client = create_redis_client(settings)
    try:
        await asyncio.wait_for(client.ping(), timeout=CONNECT_PROBE_TIMEOUT_S)
    except Exception as exc:
        await close_redis_client(client)
        if settings.redis.required:
            msg = (
                "Redis недоступен, а HIREBOT_REDIS__REQUIRED=true. "
                "Запустите Redis или снимите требование, чтобы работать на MemoryCache."
            )
            raise ConfigError(msg) from exc
        logger.warning(
            "cache_fallback_to_memory",
            reason=type(exc).__name__,
            note="кэш будет локальным для процесса; TTL и LRU сохранены",
        )
        return MemoryCache()
    logger.info("cache_redis_ready")
    return RedisCache(client)


__all__ = ["MemoryCache", "RedisCache", "create_cache"]
