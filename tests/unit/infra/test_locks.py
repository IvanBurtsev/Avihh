"""Тесты блокировок: взаимное исключение, таймаут, освобождение по токену."""

from __future__ import annotations

from collections.abc import Awaitable, Callable

import pytest

from hirebot.infra.locks import RELEASE_SCRIPT, MemoryLock, RedisLock
from hirebot.ports.infra import LockPort


async def test_memory_lock_excludes_second_holder() -> None:
    locks = MemoryLock()

    async with locks.acquire("refresh:hh", wait_s=1.0) as first:
        assert first is True
        async with locks.acquire("refresh:hh", wait_s=0.01) as second:
            assert second is False


async def test_memory_lock_released_after_exception() -> None:
    locks = MemoryLock()

    # Несколько операторов внутри pytest.raises — суть проверки: нужно убедиться,
    # что блокировка действительно была захвачена и снята после исключения.
    with pytest.raises(RuntimeError):  # noqa: PT012
        async with locks.acquire("key") as acquired:
            assert acquired is True
            raise RuntimeError("boom")

    async with locks.acquire("key", wait_s=0.01) as again:
        assert again is True


async def test_memory_lock_zero_wait_does_not_block() -> None:
    locks = MemoryLock()

    async with locks.acquire("key", wait_s=0) as first:
        assert first is True
        async with locks.acquire("key", wait_s=0) as second:
            assert second is False


async def test_memory_lock_keys_are_independent() -> None:
    locks = MemoryLock()

    async with locks.acquire("a", wait_s=0.01) as first, locks.acquire("b", wait_s=0.01) as second:
        assert first is True
        assert second is True


def test_lock_implementations_satisfy_port(fake_redis: _FakeRedis) -> None:
    assert isinstance(MemoryLock(), LockPort)
    assert isinstance(RedisLock(fake_redis), LockPort)  # type: ignore[arg-type]


async def test_redis_lock_sets_token_with_ttl(fake_redis: _FakeRedis) -> None:
    lock = RedisLock(fake_redis, prefix="lock:")  # type: ignore[arg-type]

    async with lock.acquire("key", ttl_s=30, wait_s=0.1) as acquired:
        assert acquired is True
        assert "lock:key" in fake_redis.store
        assert fake_redis.ttls["lock:key"] == 30_000

    assert fake_redis.store == {}


async def test_redis_lock_returns_false_when_busy(fake_redis: _FakeRedis) -> None:
    lock = RedisLock(fake_redis)  # type: ignore[arg-type]
    fake_redis.store["hirebot:lock:key"] = "someone-else"

    async with lock.acquire("key", wait_s=0.01) as acquired:
        assert acquired is False

    # Чужой лок не тронут.
    assert fake_redis.store["hirebot:lock:key"] == "someone-else"


async def test_redis_lock_does_not_release_foreign_token(fake_redis: _FakeRedis) -> None:
    lock = RedisLock(fake_redis)  # type: ignore[arg-type]

    async with lock.acquire("key", wait_s=0.1) as acquired:
        assert acquired is True
        # Имитируем истечение TTL: лок перешёл другому владельцу.
        fake_redis.store["hirebot:lock:key"] = "new-owner"

    assert fake_redis.store["hirebot:lock:key"] == "new-owner"


def test_release_script_compares_token() -> None:
    assert "GET" in RELEASE_SCRIPT
    assert "DEL" in RELEASE_SCRIPT
    assert "ARGV[1]" in RELEASE_SCRIPT


class _FakeRedis:
    """Двойник Redis для проверки логики лока без живого сервера."""

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttls: dict[str, int] = {}

    def register_script(self, script: str) -> Callable[..., Awaitable[int]]:
        def _factory(*, keys: list[str], args: list[str]) -> Awaitable[int]:
            return _release(self.store, keys[0], args[0])

        return _factory

    async def set(self, key: str, value: str, *, nx: bool = False, px: int | None = None) -> bool:
        if nx and key in self.store:
            return False
        self.store[key] = value
        if px is not None:
            self.ttls[key] = px
        return True


async def _release(store: dict[str, str], key: str, token: str) -> int:
    if store.get(key) == token:
        del store[key]
        return 1
    return 0


@pytest.fixture
def fake_redis() -> _FakeRedis:
    return _FakeRedis()
