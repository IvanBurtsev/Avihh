"""Тесты кэша: TTL, LRU, счётчики, JSON и деградация без Redis."""

from __future__ import annotations

from collections.abc import Callable

import pytest

from hirebot.config import Settings
from hirebot.domain.errors import ConfigError
from hirebot.infra.cache import MemoryCache, RedisCache, create_cache
from hirebot.infra.clock import FakeClock
from hirebot.ports.infra import CachePort, NullCache


class _FakeRedis:
    """Минимальный двойник Redis: только то, что использует ``RedisCache``."""

    def __init__(self) -> None:
        self.store: dict[str, bytes] = {}
        self.ttls: dict[str, int] = {}
        self.expires: list[str] = []

    async def get(self, key: str) -> bytes | None:
        return self.store.get(key)

    async def set(self, key: str, value: bytes, *, px: int | None = None, nx: bool = False) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        if px is not None:
            self.ttls[key] = px
        return True

    async def delete(self, key: str) -> int:
        return 1 if self.store.pop(key, None) is not None else 0

    async def incr(self, key: str) -> int:
        current = int(self.store.get(key, b"0")) + 1
        self.store[key] = str(current).encode()
        return current

    async def expire(self, key: str, seconds: int, *, nx: bool = False) -> bool:
        if nx and key in self.ttls:
            return False
        self.expires.append(key)
        self.ttls[key] = seconds * 1000
        return True


async def test_set_and_get_bytes(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)

    await cache.set("key", b"value")

    assert await cache.get("key") == b"value"
    assert cache.hits == 1


async def test_get_missing_returns_none(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)

    assert await cache.get("nope") is None
    assert cache.misses == 1


async def test_delete_removes_key(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)
    await cache.set("key", b"value")

    await cache.delete("key")
    await cache.delete("key")  # повторное удаление не падает

    assert await cache.get("key") is None


async def test_ttl_expires_by_virtual_clock(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)
    await cache.set("key", b"value", ttl_s=60)

    fake_clock.advance(59)
    assert await cache.get("key") == b"value"

    fake_clock.advance(2)
    assert await cache.get("key") is None
    assert cache.size == 0


async def test_non_positive_ttl_means_do_not_cache(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)

    await cache.set("key", b"value", ttl_s=0)

    assert await cache.get("key") is None


async def test_lru_eviction_keeps_hot_keys(fake_clock: FakeClock) -> None:
    cache = MemoryCache(max_keys=2, clock=fake_clock)
    await cache.set("a", b"1")
    await cache.set("b", b"2")
    assert await cache.get("a") == b"1"  # обращение делает ключ «свежим»

    await cache.set("c", b"3")

    assert await cache.get("a") == b"1"
    assert await cache.get("b") is None
    assert await cache.get("c") == b"3"
    assert cache.size == 2


async def test_lru_eviction_prefers_expired_keys(fake_clock: FakeClock) -> None:
    cache = MemoryCache(max_keys=1, clock=fake_clock)
    await cache.set("expired", b"x", ttl_s=1)
    fake_clock.advance(2)

    await cache.set("fresh", b"y")

    assert await cache.get("fresh") == b"y"
    assert cache.size == 1


async def test_json_roundtrip(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)
    payload = {"score": 87.5, "gaps": ["нет опыта"], "nested": {"a": [1, 2]}}

    await cache.set_json("json", payload)

    assert await cache.get_json("json") == payload
    assert await cache.get_json("missing") is None


async def test_incr_counter_and_ttl(fake_clock: FakeClock) -> None:
    cache = MemoryCache(clock=fake_clock)

    assert await cache.incr("hits", ttl_s=10) == 1
    assert await cache.incr("hits", ttl_s=10) == 2

    fake_clock.advance(11)
    assert await cache.incr("hits", ttl_s=10) == 1


def test_memory_cache_requires_positive_max_keys() -> None:
    with pytest.raises(ValueError, match="max_keys"):
        MemoryCache(max_keys=0)


def test_cache_implementations_satisfy_port() -> None:
    assert isinstance(MemoryCache(), CachePort)
    assert isinstance(NullCache(), CachePort)


async def test_create_cache_degrades_to_memory_without_redis(
    settings_factory: Callable[..., Settings],
) -> None:
    # Порт 1 закрыт всегда: соединение отклоняется мгновенно, сеть не нужна.
    settings = settings_factory(redis={"dsn": "redis://127.0.0.1:1/0", "required": False})

    cache = await create_cache(settings)

    assert isinstance(cache, MemoryCache)


async def test_create_cache_raises_when_redis_required(
    settings_factory: Callable[..., Settings],
) -> None:
    settings = settings_factory(redis={"dsn": "redis://127.0.0.1:1/0", "required": True})

    with pytest.raises(ConfigError, match="REQUIRED"):
        await create_cache(settings)


async def test_redis_cache_uses_prefix_and_serializes() -> None:
    client = _FakeRedis()
    cache = RedisCache(client, prefix="test:")  # type: ignore[arg-type]

    await cache.set("key", b"value", ttl_s=30)
    assert client.store["test:key"] == b"value"
    assert client.ttls["test:key"] == 30_000
    assert await cache.get("key") == b"value"

    await cache.set_json("json", {"a": 1})
    assert await cache.get_json("json") == {"a": 1}

    await cache.delete("key")
    assert "test:key" not in client.store


async def test_redis_cache_respects_non_positive_ttl() -> None:
    client = _FakeRedis()
    cache = RedisCache(client)  # type: ignore[arg-type]

    await cache.set("key", b"value", ttl_s=-1)

    assert client.store == {}


async def test_redis_cache_incr_sets_ttl_once() -> None:
    client = _FakeRedis()
    cache = RedisCache(client)  # type: ignore[arg-type]

    assert await cache.incr("hits", ttl_s=60) == 1
    assert await cache.incr("hits", ttl_s=60) == 2
    assert client.expires == ["hirebot:cache:hits"]
