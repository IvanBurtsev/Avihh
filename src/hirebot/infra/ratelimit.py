"""Token bucket: защита от 429 и от бана аккаунта площадки.

Почему именно token bucket, а не «не чаще N раз в секунду»:

* burst разрешён — поллер может отправить пачку из 5 запросов сразу, а потом
  подождать; это ровно то поведение, которое ожидают площадки;
* средняя скорость при этом ограничена сверху;
* состояние — два числа, поэтому его дешево держать в Redis и атомарно
  обновлять одним Lua-скриптом.

Локальный fallback нужен, чтобы демо и unit-тесты не требовали Redis. Он
ограничивает только текущий процесс — при нескольких воркерах нужен Redis.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any, cast

from redis.asyncio import Redis

from hirebot.config import Settings
from hirebot.infra.cache import RedisCache
from hirebot.infra.clock import SystemClock
from hirebot.logging_setup import get_logger
from hirebot.ports.infra import CachePort, ClockPort, RateLimiterPort

logger = get_logger(__name__)

#: Атомарный token bucket.
#: ARGV: rate (запросов/с), burst, now_ms, ttl_ms.
#: Возврат: 0 — токен выдан; отрицательное число — сколько мс не хватает.
#: Скрипт **никогда не ждёт** внутри Redis: ожидание делает Python, чтобы не
#: занимать единственный поток сервера длинным sleep.
TOKEN_BUCKET_SCRIPT = """
local key = KEYS[1]
local rate = tonumber(ARGV[1])
local burst = tonumber(ARGV[2])
local now_ms = tonumber(ARGV[3])
local ttl_ms = tonumber(ARGV[4])

local state = redis.call('HMGET', key, 'tokens', 'ts')
local tokens = tonumber(state[1])
local ts = tonumber(state[2])
if tokens == nil or ts == nil then
    tokens = burst
    ts = now_ms
end

local elapsed_ms = now_ms - ts
if elapsed_ms < 0 then
    elapsed_ms = 0
end
tokens = math.min(burst, tokens + (elapsed_ms / 1000.0) * rate)

local result = 0
if tokens >= 1.0 then
    tokens = tokens - 1.0
else
    result = -math.ceil(((1.0 - tokens) / rate) * 1000)
end

redis.call('HSET', key, 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', key, ttl_ms)
return result
"""

#: Ограничение на один шаг ожидания: длинный sleep не должен «залипать»,
#: если лимит поменяли на лету или часы прыгнули.
MAX_SLEEP_STEP_S = 5.0


class TokenBucketRateLimiter:
    """Token bucket с атомарным состоянием в Redis и локальным fallback.

    ``acquire`` **сам** дожидается разрешения и возвращает, сколько секунд
    пришлось ждать (0.0 — ожидания не было). При ``wait=False`` ожидания нет:
    возвращается ``-1.0``, если лимит исчерпан. Такое значение выбрано, чтобы
    вызывающая сторона не путала «подождал 0 секунд» и «не пустили».
    """

    __slots__ = ("_buckets", "_clock", "_lock", "_redis", "_script")

    def __init__(self, *, redis: Redis | None = None, clock: ClockPort | None = None) -> None:
        self._redis = redis
        self._clock: ClockPort = clock if clock is not None else SystemClock()
        self._lock = asyncio.Lock()
        # key -> (tokens, timestamp последнего пересчёта)
        self._buckets: dict[str, tuple[float, float]] = {}
        self._script: Any = redis.register_script(TOKEN_BUCKET_SCRIPT) if redis is not None else None

    async def acquire(self, key: str, *, rate_per_second: float, burst: int, wait: bool = True) -> float:
        """Получить право на один запрос."""
        if rate_per_second <= 0:
            msg = "rate_per_second должен быть > 0"
            raise ValueError(msg)
        if burst < 1:
            msg = "burst должен быть >= 1"
            raise ValueError(msg)
        if self._script is None:
            return await self._acquire_local(key, rate_per_second, burst, wait)
        return await self._acquire_redis(key, rate_per_second, burst, wait)

    async def _acquire_local(self, key: str, rate: float, burst: int, wait: bool) -> float:
        waited = 0.0
        while True:
            async with self._lock:
                now = self._clock.now().timestamp()
                tokens, ts = self._buckets.get(key, (float(burst), now))
                elapsed = max(0.0, now - ts)
                tokens = min(float(burst), tokens + elapsed * rate)
                if tokens >= 1.0:
                    self._buckets[key] = (tokens - 1.0, now)
                    return waited
                self._buckets[key] = (tokens, now)
                missing_s = (1.0 - tokens) / rate
            if not wait:
                return -1.0
            step = min(missing_s, MAX_SLEEP_STEP_S)
            await self._clock.sleep(step)
            waited += step

    async def _acquire_redis(self, key: str, rate: float, burst: int, wait: bool) -> float:
        waited = 0.0
        ttl_ms = max(1, math.ceil(burst / rate * 2000))
        full_key = f"hirebot:ratelimit:{key}"
        while True:
            now_ms = int(self._clock.now().timestamp() * 1000)
            raw = await self._script(keys=[full_key], args=[rate, burst, now_ms, ttl_ms])
            result = int(cast("int | str", raw))
            if result >= 0:
                return waited
            if not wait:
                return -1.0
            step = min(-result / 1000.0, MAX_SLEEP_STEP_S)
            await self._clock.sleep(step)
            waited += step


async def create_rate_limiter(settings: Settings, cache: CachePort) -> RateLimiterPort:
    """Собрать лимитер.

    Если кэш уже подключён к Redis — используем тот же пул соединений:
    отдельный клиент означал бы второй набор сокетов и второй набор таймаутов.
    """
    clock = SystemClock()
    if isinstance(cache, RedisCache):
        try:
            await asyncio.wait_for(cache.ping(), timeout=1.5)
        except Exception as exc:
            logger.warning("ratelimit_fallback_to_local", reason=type(exc).__name__)
            return TokenBucketRateLimiter(clock=clock)
        logger.info("ratelimit_redis_ready")
        return TokenBucketRateLimiter(redis=cache.client, clock=clock)
    logger.info("ratelimit_local", redis_required=settings.redis.required)
    return TokenBucketRateLimiter(clock=clock)


__all__ = ["MAX_SLEEP_STEP_S", "TOKEN_BUCKET_SCRIPT", "TokenBucketRateLimiter", "create_rate_limiter"]
