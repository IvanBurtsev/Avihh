"""Тесты источника времени."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

import pytest

from hirebot.infra.clock import FakeClock, SystemClock
from hirebot.ports.infra import ClockPort


def test_system_clock_returns_aware_utc() -> None:
    now = SystemClock().now()

    assert now.tzinfo is not None
    assert now.utcoffset() == timedelta(0)


async def test_system_clock_sleep_is_awaitable() -> None:
    clock = SystemClock()

    await clock.sleep(0)
    await clock.sleep(-1)


def test_fake_clock_defaults_to_fixed_moment() -> None:
    first = FakeClock()
    second = FakeClock()

    assert first.now() == second.now()
    assert first.now().tzinfo is not None


def test_fake_clock_advance_moves_time_forward(fake_clock: FakeClock) -> None:
    start = fake_clock.now()

    fake_clock.advance(90)

    assert fake_clock.now() - start == timedelta(seconds=90)


def test_fake_clock_set_replaces_value(fake_clock: FakeClock) -> None:
    moment = datetime(2026, 5, 1, 12, 0, tzinfo=UTC)

    fake_clock.set(moment)

    assert fake_clock.now() == moment


async def test_fake_clock_sleep_is_virtual(fake_clock: FakeClock) -> None:
    start = fake_clock.now()
    loop = asyncio.get_running_loop()
    before = loop.time()

    await fake_clock.sleep(3600)

    assert fake_clock.now() - start == timedelta(seconds=3600)
    assert fake_clock.slept == pytest.approx(3600)
    # Реального ожидания не было: часы цикла почти не сдвинулись.
    assert loop.time() - before < 1.0


def test_fake_clock_rejects_naive_start() -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        # Наивная дата здесь — предмет проверки, а не ошибка: часы обязаны её отвергнуть.
        FakeClock(datetime(2024, 1, 1))  # noqa: DTZ001


def test_fake_clock_rejects_backwards_advance(fake_clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="назад"):
        fake_clock.advance(-1)


def test_fake_clock_rejects_naive_set(fake_clock: FakeClock) -> None:
    with pytest.raises(ValueError, match="timezone-aware"):
        fake_clock.set(datetime(2024, 1, 1))  # noqa: DTZ001 - см. тест выше


def test_clock_implementations_satisfy_port() -> None:
    assert isinstance(SystemClock(), ClockPort)
    assert isinstance(FakeClock(), ClockPort)
