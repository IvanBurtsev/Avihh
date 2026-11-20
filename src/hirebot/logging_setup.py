"""Логирование: структурный JSON с обязательным correlation id.

Требование: по любому сообщению кандидата можно восстановить всю цепочку
(отклик -> резюме -> скрининг -> сообщение -> бронь). Поэтому каждый лог
несёт ``correlation_id`` и, если применимо, ``candidate_id``/``conversation_id``.
"""

from __future__ import annotations

import logging
import sys
from contextvars import ContextVar
from typing import Any

import structlog

_correlation_id: ContextVar[str | None] = ContextVar("correlation_id", default=None)


def bind_correlation_id(value: str) -> None:
    """Привязать correlation id к текущей задаче/запросу."""
    _correlation_id.set(value)
    structlog.contextvars.bind_contextvars(correlation_id=value)


def get_correlation_id() -> str | None:
    return _correlation_id.get()


def clear_context() -> None:
    _correlation_id.set(None)
    structlog.contextvars.clear_contextvars()


def _add_correlation_id(_logger: Any, _method: str, event_dict: dict[str, Any]) -> dict[str, Any]:
    cid = _correlation_id.get()
    if cid is not None and "correlation_id" not in event_dict:
        event_dict["correlation_id"] = cid
    return event_dict


def ensure_utf8_streams() -> None:
    """Перевести ``stdout``/``stderr`` в UTF-8.

    Консоль Windows по умолчанию работает в ANSI-кодировке (cp1251/cp866),
    и символы, которых в ней нет (например «→»), роняют процесс с
    ``UnicodeEncodeError`` вместо того, чтобы просто напечататься. Русский
    текст в cp1251 помещается, но полагаться на это нельзя: вывод может быть
    перенаправлен в файл или в пайп с другой кодировкой.
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):  # pragma: no cover - зависит от окружения
            continue


def configure_logging(*, level: str = "INFO", json_output: bool = True) -> None:
    """Настроить structlog и перехватить логи стандартной библиотеки.

    Секреты в логи не попадают: ``SecretStr`` печатается как ``**********``,
    а HTTP-тела запросов логируются только в режиме DEBUG и с редакцией.
    """
    ensure_utf8_streams()
    numeric_level = logging.getLevelNamesMapping().get(level.upper(), logging.INFO)

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        _add_correlation_id,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
    ]

    renderer: Any = (
        structlog.processors.JSONRenderer() if json_output else structlog.dev.ConsoleRenderer(colors=False)
    )

    structlog.configure(
        processors=[*shared_processors, structlog.processors.format_exc_info, renderer],
        wrapper_class=structlog.make_filtering_bound_logger(numeric_level),
        # Именно stdlib-фабрика, а не PrintLoggerFactory: процессор
        # ``add_logger_name`` обращается к ``logger.name``, которого у
        # PrintLogger нет, и падает с AttributeError на первой же записи.
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    logging.basicConfig(
        format="%(message)s",
        stream=sys.stdout,
        level=numeric_level,
        force=True,
    )
    # Шумные библиотеки приглушаем: их DEBUG бесполезен и содержит тела запросов.
    for noisy in ("httpx", "httpcore", "asyncio", "urllib3"):
        logging.getLogger(noisy).setLevel(max(numeric_level, logging.WARNING))


def get_logger(name: str) -> structlog.stdlib.BoundLogger:
    return structlog.get_logger(name)  # type: ignore[no-any-return]
