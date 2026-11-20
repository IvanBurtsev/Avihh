"""Задачи воркера: тонкие обёртки над сервисами.

Правила, которые соблюдаются здесь:

* задача **не содержит бизнес-логики** — только разрешение контейнера, вызов
  сервиса, логирование и приведение результата к JSON-виду;
* имя задачи в очереди равно имени функции (``poll_channel`` и т.д.) — это
  контракт с API, который ставит задачи через ``JobQueue.enqueue``;
* payload — плоский словарь, ``UUID`` передаются строками;
* каждая задача привязывает ``correlation_id`` (из ``ctx['job_id']``) и
  восстанавливает предыдущий контекст, чтобы inline-режим внутри HTTP-запроса
  не терял correlation id запроса.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from datetime import datetime
from typing import Any
from uuid import UUID, uuid4

from hirebot.container import Container
from hirebot.domain.enums import Channel
from hirebot.logging_setup import bind_correlation_id, clear_context, get_correlation_id, get_logger
from hirebot.worker import services
from hirebot.worker.results import conversation_state, int_counters

logger = get_logger(__name__)

#: Имена задач очереди. Должны совпадать с именами функций ниже.
JOB_POLL_CHANNEL = "poll_channel"
JOB_PROCESS_CONVERSATION = "process_conversation"
JOB_BOOK_INTERVIEW = "book_interview"
JOB_DISPATCH_OUTBOX = "dispatch_outbox"

#: Сколько откликов забирать за один цикл поллинга по умолчанию.
DEFAULT_POLL_LIMIT = 50

#: Размер пачки outbox за один проход диспетчера.
DEFAULT_OUTBOX_BATCH = 20


def container_from(ctx: Mapping[str, Any]) -> Container:
    """Достать контейнер из контекста задачи.

    Контейнер кладёт ``startup`` воркера (или CLI в inline-режиме). Явная ошибка
    лучше ``KeyError``: она объясняет, что воркер запущен неправильно.
    """
    container = ctx.get("container")
    if not isinstance(container, Container):
        msg = "В контексте задачи нет DI-контейнера: воркер запущен без startup-фазы"
        raise RuntimeError(msg)
    return container


def parse_uuid(value: object, *, field: str) -> UUID:
    """Разобрать UUID из payload задачи.

    Невалидный идентификатор — это ошибка постановки задачи, а не сбой
    обработки: повторять её бессмысленно, поэтому исключение ``ValueError``
    пробрасывается наружу и задача помечается упавшей.
    """
    if isinstance(value, UUID):
        return value
    if isinstance(value, str):
        try:
            return UUID(value)
        except ValueError as exc:
            msg = f"Некорректный {field} в задаче: {value!r}"
            raise ValueError(msg) from exc
    msg = f"Ожидался строковый {field}, получено {type(value).__name__}"
    raise ValueError(msg)


@contextmanager
def job_context(ctx: Mapping[str, Any], name: str, **fields: Any) -> Iterator[str]:
    """Контекст выполнения задачи: correlation id, старт и ошибки в логе."""
    correlation_id = str(ctx.get("job_id") or f"job-{uuid4().hex[:12]}")
    previous = get_correlation_id()
    bind_correlation_id(correlation_id)
    logger.info("job_started", job=name, correlation_id=correlation_id, **fields)
    try:
        yield correlation_id
    except Exception as exc:
        logger.exception(
            "job_failed",
            job=name,
            correlation_id=correlation_id,
            error_type=type(exc).__name__,
            **fields,
        )
        raise
    finally:
        # Восстанавливаем то, что было до задачи: inline-очередь выполняется
        # внутри HTTP-запроса и correlation id запроса терять нельзя.
        if previous is None:
            clear_context()
        else:
            bind_correlation_id(previous)


def parse_slot_start(value: object) -> datetime | None:
    """Разобрать время слота из payload задачи.

    Наивное время запрещено: слоты сравниваются между часовыми поясами, и
    ошибка здесь означает бронь не в то время. Пустое значение — допустимо
    (задача, поставленная до появления поля, адресует слот индексом).
    """
    if value is None or value == "":
        return None
    try:
        parsed = datetime.fromisoformat(str(value))
    except ValueError as exc:
        msg = f"Некорректное время слота в payload задачи: {value!r}"
        raise ValueError(msg) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        msg = f"Время слота должно содержать часовой пояс: {value!r}"
        raise ValueError(msg)
    return parsed


def _parse_channel(value: object) -> Channel:
    """Привести значение канала к перечислению ``Channel``."""
    try:
        return Channel(str(value))
    except ValueError as exc:
        allowed = ", ".join(item.value for item in Channel)
        msg = f"Неизвестный канал {value!r}; допустимые значения: {allowed}"
        raise ValueError(msg) from exc


async def poll_channel(
    ctx: Mapping[str, Any], channel: str, limit: int = DEFAULT_POLL_LIMIT
) -> dict[str, Any]:
    """Опросить площадку и поставить в работу новые отклики."""
    with job_context(ctx, JOB_POLL_CHANNEL, channel=channel) as correlation_id:
        container = container_from(ctx)
        channel_name = _parse_channel(channel)
        result = await services.ingest_service(container).poll(channel_name, limit=limit)
        counters = int_counters(result)
        logger.info("poll_channel_done", channel=channel_name.value, **counters)
        return {
            "job": JOB_POLL_CHANNEL,
            "channel": channel_name.value,
            "counters": counters,
            "correlation_id": correlation_id,
        }


async def process_conversation(ctx: Mapping[str, Any], conversation_id: str) -> dict[str, Any]:
    """Прогнать один шаг воронки по диалогу."""
    with job_context(ctx, JOB_PROCESS_CONVERSATION, conversation_id=conversation_id) as correlation_id:
        container = container_from(ctx)
        identifier = parse_uuid(conversation_id, field="conversation_id")
        result = await services.conversation_service(container).process(identifier)
        counters = int_counters(result)
        state = conversation_state(result)
        logger.info(
            "process_conversation_done",
            conversation_id=str(identifier),
            state=state.value if state is not None else None,
            **counters,
        )
        return {
            "job": JOB_PROCESS_CONVERSATION,
            "conversation_id": str(identifier),
            "state": state.value if state is not None else None,
            "counters": counters,
            "correlation_id": correlation_id,
        }


async def book_interview(
    ctx: Mapping[str, Any],
    conversation_id: str,
    slot_index: int | None = None,
    slot_start: str | None = None,
    requested_by: str = "bot",
) -> dict[str, Any]:
    """Забронировать собеседование по выбранному слоту.

    Слот адресуется **временем** (``slot_start``): это устойчиво к пересчёту
    сетки слотов между постановкой задачи и её выполнением. ``slot_index``
    поддержан для задач, поставленных до появления поля.
    """
    with job_context(
        ctx,
        JOB_BOOK_INTERVIEW,
        conversation_id=conversation_id,
        slot_index=slot_index,
        slot_start=slot_start,
    ) as correlation_id:
        container = container_from(ctx)
        identifier = parse_uuid(conversation_id, field="conversation_id")
        start = parse_slot_start(slot_start)
        booking = await services.scheduling_service(container).book(
            conversation_id=identifier,
            slot_index=None if start is not None else slot_index,
            slot_start=start,
            requested_by=requested_by,
        )
        booking_id = getattr(booking, "id", None)
        external_meeting_id = getattr(booking, "external_meeting_id", None)
        logger.info(
            "book_interview_done",
            conversation_id=str(identifier),
            slot_start=start.isoformat() if start is not None else None,
            slot_index=None if start is not None else slot_index,
            booking_id=str(booking_id) if booking_id is not None else None,
            external_meeting_id=str(external_meeting_id) if external_meeting_id is not None else None,
        )
        return {
            "job": JOB_BOOK_INTERVIEW,
            "conversation_id": str(identifier),
            "slot_index": None if start is not None else slot_index,
            "slot_start": start.isoformat() if start is not None else None,
            "booking_id": str(booking_id) if booking_id is not None else None,
            "external_meeting_id": (str(external_meeting_id) if external_meeting_id is not None else None),
            "correlation_id": correlation_id,
        }


async def dispatch_outbox(ctx: Mapping[str, Any], batch_size: int = DEFAULT_OUTBOX_BATCH) -> dict[str, Any]:
    """Доставить накопленные исходящие действия (транзакционный outbox)."""
    with job_context(ctx, JOB_DISPATCH_OUTBOX, batch_size=batch_size) as correlation_id:
        container = container_from(ctx)
        processed = await services.outbox_dispatcher(container).drain(batch_size=batch_size)
        delivered = int(processed or 0)
        logger.info("dispatch_outbox_done", delivered=delivered)
        return {
            "job": JOB_DISPATCH_OUTBOX,
            "delivered": delivered,
            "correlation_id": correlation_id,
        }


async def poll_enabled_channels(ctx: Mapping[str, Any]) -> dict[str, Any]:
    """Cron-задача: опросить все включённые в конфиге каналы.

    Ошибка одного канала не должна останавливать остальные: площадки падают
    независимо, а Avito/ХХ опрашиваются одним циклом.
    """
    container = container_from(ctx)
    summary: dict[str, int] = {}
    for channel in container.enabled_channels:
        try:
            result = await poll_channel(ctx, channel.value)
        except Exception:
            # Ошибка уже залогирована в poll_channel; здесь только помечаем канал.
            logger.warning("poll_channel_skipped", channel=channel.value)
            summary[channel.value] = -1
            continue
        summary[channel.value] = int(result["counters"].get("fetched", 0))
    logger.info("poll_enabled_channels_done", channels=len(container.enabled_channels))
    return {"job": "poll_enabled_channels", "fetched": summary}


__all__ = [
    "DEFAULT_OUTBOX_BATCH",
    "DEFAULT_POLL_LIMIT",
    "JOB_BOOK_INTERVIEW",
    "JOB_DISPATCH_OUTBOX",
    "JOB_POLL_CHANNEL",
    "JOB_PROCESS_CONVERSATION",
    "book_interview",
    "container_from",
    "dispatch_outbox",
    "job_context",
    "parse_uuid",
    "poll_channel",
    "poll_enabled_channels",
    "process_conversation",
]
