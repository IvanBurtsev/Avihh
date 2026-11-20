"""Процессный fallback для ``LockPort``.

Зачем нужен: single-flight обновление OAuth-токенов обязательно (refresh-токен
одноразовый), но ``LockPort.dispatch`` не входит в замороженные сигнатуры
``build_channels``/``build_calendar``. Если инфраструктурный (Redis) лок не
передан, адаптер обязан работать корректно — поэтому используется локальный
``asyncio.Lock`` на ключ.

Ограничение честно фиксируем: это лок **внутри процесса**. При нескольких
воркерах нужен ``LockPort`` из ``infra`` (Redis) — иначе два процесса обновят
токен одновременно и потеряют сессию. Поэтому все фабрики адаптеров принимают
необязательный ``locks`` и прокидывают его сюда.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

__all__ = ["LocalLock"]


class LocalLock:
    """Реализация ``LockPort`` через ``asyncio.Lock`` по ключу."""

    def __init__(self) -> None:
        self._locks: dict[str, asyncio.Lock] = {}

    def _lock_for(self, key: str) -> asyncio.Lock:
        lock = self._locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[key] = lock
        return lock

    @asynccontextmanager
    async def acquire(
        self,
        key: str,
        *,
        ttl_s: float = 30.0,  # noqa: ARG002 — лок процессный, TTL неприменим
        wait_s: float = 5.0,
    ) -> AsyncIterator[bool]:
        """Захватить лок на ключ.

        Возвращает ``True``, если лок получен, и ``False`` при таймауте ожидания:
        вызывающая сторона обязана обработать ``False`` (подхватить результат
        другого воркера из кэша или отдать ретраябельную ошибку), а не считать,
        что критическая секция выполнена.
        """
        lock = self._lock_for(key)
        try:
            await asyncio.wait_for(lock.acquire(), timeout=wait_s)
        except TimeoutError:
            yield False
            return
        try:
            yield True
        finally:
            lock.release()
