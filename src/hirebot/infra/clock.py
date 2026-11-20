"""Источник времени и сна: ``ClockPort`` без прямых ``datetime.now()``.

Зачем отдельный порт: время — это внешняя зависимость, и она должна быть
подменяемой. Тест, который проверяет «через 30 минут воркер поднимет задачу»,
не обязан ждать 30 минут. Плюс правило проекта — только timezone-aware UTC
(``ruff`` rule ``DTZ``), поэтому наивные даты здесь физически не создаются.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta

#: Начало виртуального времени ``FakeClock``: фиксированная точка, чтобы тесты
#: были детерминированными и не зависели от даты запуска.
DEFAULT_FAKE_START = datetime(2024, 1, 1, 9, 0, tzinfo=UTC)


class SystemClock:
    """Реальные часы процесса: ``datetime.now(UTC)`` и настоящий ``asyncio.sleep``."""

    __slots__ = ()

    def now(self) -> datetime:
        """Текущее время в UTC (aware)."""
        return datetime.now(tz=UTC)

    async def sleep(self, seconds: float) -> None:
        """Реально подождать ``seconds`` секунд."""
        if seconds > 0:
            await asyncio.sleep(seconds)


class FakeClock:
    """Виртуальное время для тестов.

    ``sleep`` **не спит по-настоящему**: он сдвигает виртуальные часы вперёд и
    сразу возвращает управление. Это даёт двум проверкам быть одновременно
    честными и быстрыми:

    * экспоненциальный backoff ретраев действительно выдерживает паузу —
      её видно в ``slept`` и в ``now()``;
    * «через час» наступает мгновенно через :meth:`advance`.

    Внимание: ``sleep`` отдаёт управление через ``await asyncio.sleep(0)``,
    поэтому ждать наступления момента нужно по ``now()``, а не по факту сна.
    """

    __slots__ = ("_now", "_slept")

    def __init__(self, start: datetime | None = None) -> None:
        initial = start if start is not None else DEFAULT_FAKE_START
        if initial.tzinfo is None or initial.tzinfo.utcoffset(initial) is None:
            msg = (
                "FakeClock требует timezone-aware начальное время (например datetime(2024, 1, 1, tzinfo=UTC))"
            )
            raise ValueError(msg)
        self._now = initial
        self._slept = 0.0

    @property
    def slept(self) -> float:
        """Суммарно «проспанное» виртуальное время (секунды)."""
        return self._slept

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: float) -> None:
        """Сдвинуть виртуальное время вперёд."""
        if seconds < 0:
            msg = "Виртуальное время нельзя двигать назад"
            raise ValueError(msg)
        self._now += timedelta(seconds=seconds)

    def set(self, value: datetime) -> None:
        """Установить конкретный момент времени."""
        if value.tzinfo is None or value.tzinfo.utcoffset(value) is None:
            msg = "FakeClock.set требует timezone-aware значение"
            raise ValueError(msg)
        self._now = value

    async def sleep(self, seconds: float) -> None:
        if seconds <= 0:
            return
        self._slept += seconds
        self.advance(seconds)
        await asyncio.sleep(0)


__all__ = ["DEFAULT_FAKE_START", "FakeClock", "SystemClock"]
