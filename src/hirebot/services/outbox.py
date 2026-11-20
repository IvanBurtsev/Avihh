"""Диспетчер транзакционного outbox.

Outbox решает две проблемы, которые иначе не решаются одновременно
(ARCHITECTURE.md, п. 2.3):

* «отправили в чат, упали до коммита» → кандидат получил ответ, в БД его нет;
* «закоммитили, упали до отправки» → в БД есть, кандидат ничего не получил.

Поэтому бизнес-шаг сначала попадает в ``outbox`` в одной транзакции с
изменением состояния, а доставляет его этот диспетчер.

Ретраи: экспоненциальная задержка (30s, 60s, 120s...) с потолком в час, после
``settings.queue.max_attempts`` попыток запись помечается ``DEAD`` и пишется
``error`` в лог — такую задачу разбирает человек.

Идемпотентность обработки держится на двух вещах: ``claim_batch`` забирает
запись с блокировкой строки (параллельный воркер её не увидит), а ключи
идемпотентности внутри шагов (отправка сообщения, бронь слота) не дают
побочным эффектам продублироваться при повторе.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from datetime import datetime
from typing import Any
from uuid import UUID

from hirebot.container import Container
from hirebot.domain.entities import OutboxEntry
from hirebot.domain.enums import Channel, OutboxStatus
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_outbox
from hirebot.services.base import ServiceBase
from hirebot.services.conversation import ConversationService
from hirebot.services.ingest import IngestService
from hirebot.services.scheduling import SchedulingService
from hirebot.services.topics import (
    BOOK_INTERVIEW_TOPIC,
    DISPATCH_OUTBOX_TOPIC,
    POLL_CHANNEL_TOPIC,
    PROCESS_CONVERSATION_TOPIC,
    SEND_MESSAGE_TOPIC,
)

logger = get_logger(__name__)

#: Сколько записей забираем за один проход.
DEFAULT_BATCH_SIZE = 20

#: База экспоненциальной задержки повтора.
BASE_RETRY_DELAY_S = 30.0

#: Потолок задержки повтора.
MAX_RETRY_DELAY_S = 3600.0

PayloadHandler = Callable[[dict[str, Any]], Awaitable[None]]


def backoff_delay_s(
    attempt: int,
    *,
    base_s: float = BASE_RETRY_DELAY_S,
    max_s: float = MAX_RETRY_DELAY_S,
) -> float:
    """Экспоненциальная задержка повтора: ``base * 2^(attempt-1)`` с потолком."""
    safe_attempt = max(1, attempt)
    return float(min(max_s, base_s * (2 ** (safe_attempt - 1))))


class OutboxDispatcher(ServiceBase):
    """Забрать пачку задач из outbox и выполнить их."""

    def __init__(self, container: Container) -> None:
        super().__init__(container)
        self._ingest = IngestService(container)
        self._conversation = ConversationService(container)
        self._scheduling = SchedulingService(container)
        self._handlers: dict[str, PayloadHandler] = {
            POLL_CHANNEL_TOPIC: self._handle_poll_channel,
            PROCESS_CONVERSATION_TOPIC: self._handle_process_conversation,
            BOOK_INTERVIEW_TOPIC: self._handle_book_interview,
            DISPATCH_OUTBOX_TOPIC: self._handle_dispatch_outbox,
            SEND_MESSAGE_TOPIC: self._handle_send_message,
        }

    async def drain(self, *, batch_size: int = DEFAULT_BATCH_SIZE) -> int:
        """Обработать одну пачку записей outbox.

        Возвращает число **успешно** обработанных записей: вызывающая сторона
        (воркер или демо-CLI) может крутить ``drain`` до нуля и видеть прогресс.
        """
        now = self.now()
        async with self.uow() as uow:
            entries = list(await uow.outbox.claim_batch(limit=batch_size, now=now))
            # Фиксируем факт захвата (счётчик попыток, блокировка строки):
            # без коммита запись вернётся в пул и будет обработана дважды.
            await uow.commit()

        processed = 0
        for entry in entries:
            if await self._process_entry(entry):
                processed += 1
        # Метрика очереди: видно, когда outbox растёт и задачи упираются в DEAD.
        record_outbox("claimed", size=len(entries))
        if entries:
            logger.info("outbox_drained", claimed=len(entries), processed=processed)
        return processed

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #

    async def _process_entry(self, entry: OutboxEntry) -> bool:
        try:
            await self._dispatch(entry)
        except Exception as exc:
            await self._handle_failure(entry, exc)
            return False

        async with self.uow() as uow:
            # Ключ фиксируем вместе с отметкой SENT: отметка «обработано»
            # становится транзакционной и не теряется при падении между шагами.
            acquired = await uow.idempotency.acquire(f"outbox:{entry.dedupe_key}", scope=entry.topic)
            await uow.outbox.mark(entry.id, OutboxStatus.SENT)
            await uow.commit()
        if not acquired:
            logger.info(
                "outbox_entry_already_processed",
                entry_id=str(entry.id),
                topic=entry.topic,
                dedupe_key=entry.dedupe_key,
            )
        return True

    async def _dispatch(self, entry: OutboxEntry) -> None:
        handler = self._handlers.get(entry.topic)
        if handler is None:
            # Топик из более новой версии: не роняем воркер, но и молчать нельзя.
            logger.warning("outbox_unknown_topic", topic=entry.topic, entry_id=str(entry.id))
            return
        await handler(dict(entry.payload or {}))

    async def _handle_failure(self, entry: OutboxEntry, exc: Exception) -> None:
        error = f"{type(exc).__name__}: {exc}"
        max_attempts = self.settings.queue.max_attempts
        if entry.attempts >= max_attempts:
            async with self.uow() as uow:
                await uow.outbox.mark(entry.id, OutboxStatus.DEAD, error=error)
                await uow.commit()
            logger.error(
                "outbox_task_dead",
                entry_id=str(entry.id),
                topic=entry.topic,
                dedupe_key=entry.dedupe_key,
                attempts=entry.attempts,
                max_attempts=max_attempts,
                error=error,
            )
            return

        delay = backoff_delay_s(entry.attempts)
        async with self.uow() as uow:
            await uow.outbox.mark(entry.id, OutboxStatus.FAILED, error=error)
            await uow.commit()
        logger.warning(
            "outbox_task_failed",
            entry_id=str(entry.id),
            topic=entry.topic,
            attempt=entry.attempts,
            max_attempts=max_attempts,
            retry_in_s=delay,
            error=error,
        )
        await self._schedule_retry(entry, delay)

    async def _schedule_retry(self, entry: OutboxEntry, delay_s: float) -> None:
        """Запланировать повторный проход диспетчера с задержкой.

        Паузу обеспечивает **хранилище**: ``OutboxRepository.mark`` отодвигает
        ``available_at`` на экспоненциальный backoff, а ``claim_batch`` берёт
        только уже доступные записи. Отложенная задача очереди здесь — лишь
        «тик», который разбудит воркер после паузы; обойти её очередным
        ``drain`` нельзя.
        """
        try:
            await self._container.queue.enqueue(
                DISPATCH_OUTBOX_TOPIC,
                {"entry_id": str(entry.id)},
                dedupe_key=f"outbox-retry:{entry.id}:{entry.attempts}",
                delay_s=delay_s,
            )
        except Exception as exc:
            logger.warning(
                "outbox_retry_schedule_failed",
                entry_id=str(entry.id),
                error=str(exc),
            )

    # ------------------------------------------------------------------ #
    # Обработчики топиков
    # ------------------------------------------------------------------ #

    async def _handle_poll_channel(self, payload: dict[str, Any]) -> None:
        channel = Channel(str(payload.get("channel", "")))
        limit = payload_int(payload, "limit", 50)
        result = await self._ingest.poll(channel, limit=limit)
        logger.info(
            "outbox_poll_channel",
            channel=channel.value,
            found=result.found,
            created=result.created,
            duplicates=result.duplicates,
            errors=len(result.errors),
        )

    async def _handle_process_conversation(self, payload: dict[str, Any]) -> None:
        conversation_id = payload_uuid(payload, "conversation_id")
        outcome = await self._conversation.process(conversation_id)
        logger.info(
            "outbox_process_conversation",
            conversation_id=str(conversation_id),
            state=outcome.state.value,
            action=outcome.action_taken,
            used_llm=outcome.used_llm,
        )

    async def _handle_book_interview(self, payload: dict[str, Any]) -> None:
        """Забронировать слот, адресуя его **временем**, а не индексом сетки.

        ``slot_start`` (ISO, aware) — предпочтительная форма: при повторном
        разборе задачи сетка могла сдвинуться, и индекс указывал бы уже на
        другое время. ``slot_index`` оставлен для обратной совместимости с
        задачами, поставленными до появления поля.
        """
        conversation_id = payload_uuid(payload, "conversation_id")
        slot_start = payload_datetime(payload, "slot_start")
        slot_index = None if slot_start is not None else payload_int(payload, "slot_index", 0)
        requested_by = str(payload.get("requested_by", "worker"))
        booking = await self._scheduling.book(
            conversation_id=conversation_id,
            slot_index=slot_index,
            slot_start=slot_start,
            requested_by=requested_by,
        )
        logger.info(
            "outbox_book_interview",
            conversation_id=str(conversation_id),
            booking_id=str(booking.id),
            start=booking.start.isoformat(),
            requested_by=requested_by,
        )

    async def _handle_send_message(self, payload: dict[str, Any]) -> None:
        channel = Channel(str(payload.get("channel", "")))
        conversation_external_id = str(payload.get("conversation_external_id", ""))
        idempotency_key = str(payload.get("idempotency_key", ""))
        if not conversation_external_id or not idempotency_key:
            msg = "send_message требует conversation_external_id и idempotency_key"
            raise ValueError(msg)
        kind_hint = payload.get("kind")
        sent = await self._container.channel(channel).send_message(
            conversation_external_id,
            str(payload.get("text", "")),
            idempotency_key=idempotency_key,
            kind_hint=str(kind_hint) if kind_hint else None,
        )
        logger.info(
            "outbox_send_message",
            channel=channel.value,
            conversation_external_id=conversation_external_id,
            external_id=sent.external_id,
        )

    async def _handle_dispatch_outbox(self, payload: dict[str, Any]) -> None:
        """Задача-триггер: сам outbox разбирает воркер циклом ``drain``.

        Вложенный ``drain`` здесь был бы рекурсией без пользы (мы и так внутри
        разбора пачки), поэтому задача считается выполненной: она лишь
        «подталкивает» цикл, когда запись стала доступной после задержки.
        """
        logger.info("outbox_dispatch_tick", entry_id=str(payload.get("entry_id", "")))


def payload_uuid(payload: dict[str, Any], key: str) -> UUID:
    """Прочитать UUID из payload задачи (в очереди он лежит строкой)."""
    raw = payload.get(key)
    if raw is None or raw == "":
        msg = f"В payload задачи нет обязательного поля {key!r}"
        raise ValueError(msg)
    try:
        return UUID(str(raw))
    except ValueError as exc:
        msg = f"Поле {key!r} не является UUID: {raw!r}"
        raise ValueError(msg) from exc


def payload_int(payload: dict[str, Any], key: str, default: int) -> int:
    """Прочитать целое из payload задачи."""
    raw = payload.get(key, default)
    try:
        return int(raw)
    except (TypeError, ValueError) as exc:
        msg = f"Поле {key!r} не является целым числом: {raw!r}"
        raise ValueError(msg) from exc


def payload_datetime(payload: dict[str, Any], key: str) -> datetime | None:
    """Прочитать timezone-aware момент времени из payload задачи.

    ``None`` означает «поля нет» — вызывающий код тогда пользуется
    обратно совместимой формой (``slot_index``). Наивная дата — ошибка
    постановки задачи: без зоны невозможно понять, о каком моменте речь.
    """
    raw = payload.get(key)
    if raw is None or raw == "":
        return None
    if isinstance(raw, datetime):
        parsed = raw
    else:
        try:
            parsed = datetime.fromisoformat(str(raw))
        except ValueError as exc:
            msg = f"Поле {key!r} не является ISO-датой: {raw!r}"
            raise ValueError(msg) from exc
    if parsed.tzinfo is None or parsed.tzinfo.utcoffset(parsed) is None:
        msg = f"Поле {key!r} должно содержать timezone-aware дату: {raw!r}"
        raise ValueError(msg)
    return parsed
