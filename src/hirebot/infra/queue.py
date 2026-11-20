"""Очередь фоновых задач: arq (продакшен) и inline (демо, тесты).

Смысл порта ``JobQueue`` — один и тот же сервисный код ставит задачу одинаково,
а «где и когда она выполнится» решает конфигурация. Поэтому:

* ``InlineJobQueue`` честно ``await``-ит обработчик в текущем процессе. Это не
  «эмуляция очереди», а полноценный режим: демо и интеграционные тесты проходят
  весь пайплайн без Redis и без воркера.
* ``ArqJobQueue`` кладёт задачу в Redis. ``dedupe_key`` реализован через
  ``SET NX``: параллельный поллинг не должен поставить один и тот же шаг
  воронки дважды (иначе кандидат получит два одинаковых сообщения).
"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import Awaitable, Callable
from typing import Any

import orjson
from redis.asyncio import Redis

from hirebot.config import Settings
from hirebot.domain.errors import ConfigError, NotFound
from hirebot.infra.clock import SystemClock
from hirebot.infra.redis_client import close_redis_client, create_redis_client_from_dsn
from hirebot.logging_setup import get_logger
from hirebot.ports.infra import ClockPort, JobQueue

logger = get_logger(__name__)

#: Обработчик задачи: получает payload и делает работу. Контекст (воркер, БД)
#: обработчик берёт из замыкания — так проще тестировать без arq.
JobHandler = Callable[[dict[str, Any]], Awaitable[None]]

#: Сколько живёт маркер дедупликации. Сутки: поллинг за это время гарантированно
#: не увидит ту же самую задачу повторно, а ключи не растут бесконечно.
DEDUPE_TTL_S = 86_400.0

_DEDUPE_PREFIX = "hirebot:job-dedupe:"


def encode_payload(payload: dict[str, Any]) -> str:
    """Сериализовать payload в JSON (arq передаёт строку, а не объект)."""
    return orjson.dumps(payload).decode("utf-8")


def decode_payload(raw: str | bytes) -> dict[str, Any]:
    """Разобрать payload из JSON. Ошибка формата — ошибка программы, а не данных."""
    parsed: Any = orjson.loads(raw)
    if not isinstance(parsed, dict):
        msg = "payload задачи должен быть JSON-объектом"
        raise ValueError(msg)
    return {str(key): value for key, value in parsed.items()}


class InlineJobQueue:
    """Очередь «в текущем процессе»: задача выполняется сразу и по-настоящему.

    Особенности, которые важны для честности режима:

    * ``await handler(payload)`` — вызывающая сторона видит ошибку обработчика.
      В продакшене её увидит воркер и сделает retry, здесь — тест.
    * маркер дедупликации живёт в памяти и **снимается**, если обработчик упал:
      иначе повтор после падения был бы невозможен.
    """

    __slots__ = ("_clock", "_closed", "_handlers", "_lock", "_seen")

    def __init__(self, *, clock: ClockPort | None = None) -> None:
        self._handlers: dict[str, JobHandler] = {}
        self._seen: set[str] = set()
        self._lock = asyncio.Lock()
        self._clock: ClockPort = clock if clock is not None else SystemClock()
        self._closed = False

    @property
    def registered(self) -> tuple[str, ...]:
        """Имена зарегистрированных задач (для диагностики и тестов)."""
        return tuple(self._handlers)

    def register(self, job_name: str, handler: JobHandler) -> None:
        """Зарегистрировать обработчик. Повторная регистрация перетирает старый."""
        self._handlers[job_name] = handler

    async def enqueue(
        self,
        job_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        delay_s: float = 0.0,
    ) -> str | None:
        """Выполнить задачу сразу. ``None`` — задача уже выполнялась (dedupe)."""
        if self._closed:
            msg = "Очередь остановлена: новые задачи не принимаются"
            raise RuntimeError(msg)
        handler = self._handlers.get(job_name)
        if handler is None:
            msg = (
                f"Обработчик задачи {job_name!r} не зарегистрирован. "
                f"Зарегистрированные: {sorted(self._handlers)}"
            )
            raise NotFound(msg)
        if dedupe_key is not None:
            async with self._lock:
                if dedupe_key in self._seen:
                    logger.debug("job_deduplicated", job=job_name, dedupe_key=dedupe_key)
                    return None
                self._seen.add(dedupe_key)
        job_id = f"inline-{uuid.uuid4().hex}"
        if delay_s > 0:
            await self._clock.sleep(delay_s)
        try:
            await handler(payload)
        except BaseException:
            if dedupe_key is not None:
                async with self._lock:
                    self._seen.discard(dedupe_key)
            raise
        logger.debug("job_done", job=job_name, job_id=job_id)
        return job_id

    async def start(self) -> None:
        """Совместимость с портом: inline-очереди запускать нечего."""
        self._closed = False

    async def close(self) -> None:
        self._closed = True
        self._handlers.clear()

    async def aclose(self) -> None:
        await self.close()


class ArqJobQueue:
    """Очередь на arq/Redis.

    Клиент создаётся лениво: контейнер собирается и без Redis, а падение
    Redis не должно ломать импорт приложения. Дедупликация — ``SET NX`` с TTL,
    потому что ``_job_id`` в arq защищает только от одинаковых id, а нам нужно
    гасить повторную постановку того же бизнес-шага.
    """

    __slots__ = (
        "_dedupe_ttl_s",
        "_handlers",
        "_job_timeout_s",
        "_lock",
        "_max_attempts",
        "_pool",
        "_queue_name",
        "_redis",
        "_redis_dsn",
    )

    def __init__(
        self,
        *,
        redis_dsn: str,
        queue_name: str = "hirebot",
        max_attempts: int = 5,
        job_timeout_s: int = 300,
        dedupe_ttl_s: float = DEDUPE_TTL_S,
    ) -> None:
        self._redis_dsn = redis_dsn
        self._queue_name = queue_name
        self._max_attempts = max_attempts
        self._job_timeout_s = job_timeout_s
        self._dedupe_ttl_s = dedupe_ttl_s
        self._handlers: dict[str, JobHandler] = {}
        self._lock = asyncio.Lock()
        self._redis: Redis | None = None
        self._pool: Any = None

    @property
    def registered(self) -> tuple[str, ...]:
        return tuple(self._handlers)

    def register(self, job_name: str, handler: JobHandler) -> None:
        self._handlers[job_name] = handler

    async def start(self) -> None:
        """Установить соединение с Redis (лениво, один раз)."""
        await self._ensure_connected()

    async def _ensure_connected(self) -> Redis:
        async with self._lock:
            if self._redis is not None:
                return self._redis
            # Импорт внутри: при QUEUE__BACKEND=inline зависимость от arq не нужна.
            from arq import create_pool
            from arq.connections import RedisSettings

            redis = create_redis_client_from_dsn(self._redis_dsn)
            try:
                await redis.ping()
                self._pool = await create_pool(RedisSettings.from_dsn(self._redis_dsn))
            except Exception as exc:
                await _safe_close(redis)
                msg = (
                    "Очередь arq недоступна: Redis не отвечает. "
                    "Для демо используйте HIREBOT_QUEUE__BACKEND=inline."
                )
                raise ConfigError(msg) from exc
            self._redis = redis
            logger.info("queue_arq_ready", queue=self._queue_name)
            return redis

    async def enqueue(
        self,
        job_name: str,
        payload: dict[str, Any],
        *,
        dedupe_key: str | None = None,
        delay_s: float = 0.0,
    ) -> str | None:
        """Поставить задачу в Redis. ``None`` — задача уже стояла (dedupe)."""
        if job_name not in self._handlers:
            msg = (
                f"Обработчик задачи {job_name!r} не зарегистрирован: "
                f"воркер не сможет её выполнить. Зарегистрированные: {sorted(self._handlers)}"
            )
            raise NotFound(msg)
        redis = await self._ensure_connected()
        dedupe_redis_key = f"{_DEDUPE_PREFIX}{job_name}:{dedupe_key}" if dedupe_key else None
        if dedupe_redis_key is not None:
            acquired = await redis.set(
                dedupe_redis_key, b"1", nx=True, px=max(1, int(self._dedupe_ttl_s * 1000))
            )
            if not acquired:
                logger.debug("job_deduplicated", job=job_name, dedupe_key=dedupe_key)
                return None
        job_id = f"{job_name}:{uuid.uuid4().hex}"
        try:
            job = await self._pool.enqueue_job(
                job_name,
                encode_payload(payload),
                _job_id=job_id,
                _queue_name=self._queue_name,
                _defer_by=delay_s if delay_s > 0 else None,
            )
        except Exception:
            if dedupe_redis_key is not None:
                await redis.delete(dedupe_redis_key)
            raise
        if job is None:  # arq отказался (job_id уже существует) — снимаем маркер
            if dedupe_redis_key is not None:
                await redis.delete(dedupe_redis_key)
            return None
        logger.debug("job_enqueued", job=job_name, job_id=job_id, delay_s=delay_s)
        return job_id

    async def close(self) -> None:
        """Закрыть соединения (вызывается при остановке приложения)."""
        pool = self._pool
        redis = self._redis
        self._pool = None
        self._redis = None
        if pool is not None:
            try:
                await pool.aclose()
            except Exception:
                logger.warning("queue_pool_close_failed")
        if redis is not None:
            await _safe_close(redis)
        self._handlers.clear()

    async def aclose(self) -> None:
        await self.close()


async def _safe_close(client: Redis) -> None:
    await close_redis_client(client)


async def create_queue(settings: Settings) -> JobQueue:
    """Собрать очередь по настройкам.

    ``inline`` — режим по умолчанию: демо и тесты проходят пайплайн целиком без
    Redis. ``arq`` включается явно (``HIREBOT_QUEUE__BACKEND=arq``).
    """
    if settings.queue.backend == "arq":
        return ArqJobQueue(
            redis_dsn=settings.redis.dsn,
            queue_name=settings.queue.queue_name,
            max_attempts=settings.queue.max_attempts,
            job_timeout_s=settings.queue.job_timeout_s,
        )
    logger.info("queue_inline")
    return InlineJobQueue()


__all__ = [
    "DEDUPE_TTL_S",
    "ArqJobQueue",
    "InlineJobQueue",
    "JobHandler",
    "create_queue",
    "decode_payload",
    "encode_payload",
]
