"""Тесты token bucket: burst, ожидание, отказ при ``wait=False``."""

from __future__ import annotations

import pytest

from hirebot.infra.clock import FakeClock
from hirebot.infra.ratelimit import TokenBucketRateLimiter
from hirebot.ports.infra import RateLimiterPort


async def test_burst_is_allowed_without_waiting(fake_clock: FakeClock) -> None:
    limiter = TokenBucketRateLimiter(clock=fake_clock)

    waited = [await limiter.acquire("hh", rate_per_second=1.0, burst=3) for _ in range(3)]

    assert waited == [0.0, 0.0, 0.0]
    assert fake_clock.slept == 0.0


async def test_bucket_waits_for_refill(fake_clock: FakeClock) -> None:
    limiter = TokenBucketRateLimiter(clock=fake_clock)
    for _ in range(2):
        await limiter.acquire("hh", rate_per_second=1.0, burst=2)

    waited = await limiter.acquire("hh", rate_per_second=1.0, burst=2)

    # Не хватало ровно одного токена при 1 rps -> виртуальная секунда ожидания.
    assert waited == pytest.approx(1.0)
    assert fake_clock.slept == pytest.approx(1.0)


async def test_wait_false_returns_minus_one(fake_clock: FakeClock) -> None:
    limiter = TokenBucketRateLimiter(clock=fake_clock)
    await limiter.acquire("hh", rate_per_second=1.0, burst=1)

    result = await limiter.acquire("hh", rate_per_second=1.0, burst=1, wait=False)

    assert result == -1.0
    assert fake_clock.slept == 0.0


async def test_buckets_are_isolated_by_key(fake_clock: FakeClock) -> None:
    limiter = TokenBucketRateLimiter(clock=fake_clock)
    await limiter.acquire("hh", rate_per_second=1.0, burst=1)

    # Другая площадка имеет собственный bucket и не ждёт чужой лимит.
    assert await limiter.acquire("avito", rate_per_second=1.0, burst=1) == 0.0


async def test_burst_limit_is_respected(fake_clock: FakeClock) -> None:
    limiter = TokenBucketRateLimiter(clock=fake_clock)
    for _ in range(5):
        await limiter.acquire("hh", rate_per_second=100.0, burst=5)

    # Запас не превышает burst: даже после долгого простоя больше 5 сразу нельзя.
    fake_clock.advance(3600)
    for _ in range(5):
        assert await limiter.acquire("hh", rate_per_second=100.0, burst=5) == 0.0
    assert await limiter.acquire("hh", rate_per_second=100.0, burst=5, wait=False) == -1.0


async def test_invalid_arguments_rejected(fake_clock: FakeClock) -> None:
    limiter = TokenBucketRateLimiter(clock=fake_clock)

    with pytest.raises(ValueError, match="rate_per_second"):
        await limiter.acquire("hh", rate_per_second=0, burst=1)
    with pytest.raises(ValueError, match="burst"):
        await limiter.acquire("hh", rate_per_second=1.0, burst=0)


def test_limiter_satisfies_port() -> None:
    assert isinstance(TokenBucketRateLimiter(), RateLimiterPort)
