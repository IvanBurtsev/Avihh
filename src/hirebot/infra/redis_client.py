"""Единственная точка создания клиента Redis.

Кэш, распределённые блокировки, token bucket и очередь используют один и тот же
DSN, но **разные** клиенты: у них разные таймауты и разные требования к
декодированию ответов (кэш хранит байты, локи — токены). Общий конструктор
избавляет от расхождений в настройках подключения.
"""

from __future__ import annotations

from typing import cast

from redis.asyncio import Redis

from hirebot.config import Settings

#: Быстрый отказ при недоступном Redis: падать на старте из-за него нельзя
#: (см. деградацию в архитектуре), поэтому таймаут короткий.
DEFAULT_CONNECT_TIMEOUT_S = 1.0
DEFAULT_SOCKET_TIMEOUT_S = 5.0


def create_redis_client(settings: Settings) -> Redis:
    """Создать клиент Redis по настройкам (без сетевого подключения).

    Подключение ленивое: ``Redis.from_url`` не устанавливает соединение, поэтому
    создание клиента безопасно даже при отсутствии Redis.
    """
    return create_redis_client_from_dsn(settings.redis.dsn)


def create_redis_client_from_dsn(dsn: str) -> Redis:
    """Создать клиент по DSN. Нужен очереди и разовым подключениям."""
    # redis-py не поставляет стабы, поэтому ``from_url`` типизирован как Any.
    return cast(
        "Redis",
        Redis.from_url(
            dsn,
            encoding="utf-8",
            # Ответы кэша должны быть байтами: в них лежит orjson-полезная нагрузка.
            decode_responses=False,
            socket_connect_timeout=DEFAULT_CONNECT_TIMEOUT_S,
            socket_timeout=DEFAULT_SOCKET_TIMEOUT_S,
            health_check_interval=30,
        ),
    )


async def close_redis_client(client: Redis) -> None:
    """Закрыть клиент, не роняя вызывающий код при ошибке соединения."""
    try:
        await client.aclose()
    except Exception:
        return


__all__ = [
    "DEFAULT_CONNECT_TIMEOUT_S",
    "DEFAULT_SOCKET_TIMEOUT_S",
    "close_redis_client",
    "create_redis_client",
    "create_redis_client_from_dsn",
]
