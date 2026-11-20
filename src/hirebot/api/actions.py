"""Прикладные действия и проверки слоя доставки.

Здесь живёт то, что не относится ни к одному из сервисов, но и не должно
размазываться по роутерам:

* :func:`require_channel` — единая проверка «канал включён» (``503``, а не
  ``KeyError`` с трассировкой);
* :func:`send_manual_message` — ручное сообщение рекрутера.

Почему ручное сообщение собирается здесь, а не вызывается у сервиса: исходящее
всегда идёт через транзакционный outbox (см. ARCHITECTURE.md, п. 2.3). Мы пишем
``messages`` + ``outbox`` одной транзакцией, а доставляет запись воркер
(``dispatch_outbox``). Побочный эффект правильный: если воркер лежит, сообщение
не теряется, а ждёт в outbox.
"""

from __future__ import annotations

from dataclasses import dataclass
from uuid import UUID

from hirebot.container import Container
from hirebot.domain.entities import Message, OutboxEntry, new_id
from hirebot.domain.enums import Channel, MessageDirection, MessageKind
from hirebot.domain.errors import ChannelNotConfigured, NotFound
from hirebot.logging_setup import get_logger
from hirebot.worker.tasks import JOB_DISPATCH_OUTBOX

logger = get_logger(__name__)

#: Топик outbox для отправки сообщения. Должен совпадать с тем, что разбирает
#: ``OutboxDispatcher``: список топиков — часть контракта сервисного слоя.
OUTBOX_TOPIC_SEND_MESSAGE = "send_message"


@dataclass(slots=True, frozen=True)
class ManualMessageResult:
    """Что именно попало в БД при отправке ручного сообщения."""

    message_id: UUID
    outbox_entry_id: UUID


def require_channel(container: Container, channel: Channel) -> None:
    """Убедиться, что канал включён в конфиге.

    ``KeyError`` из ``Container.channel`` превратился бы в 500 с трассировкой;
    выключенная площадка — штатная ситуация, и клиент должен получить 503.
    """
    if channel not in container.channels:
        msg = f"Канал {channel.value!r} не сконфигурирован или отключён в настройках"
        raise ChannelNotConfigured(msg)


async def send_manual_message(
    container: Container,
    *,
    conversation_id: UUID,
    text: str,
    requested_by: str,
) -> ManualMessageResult:
    """Поставить ручное сообщение рекрутера в outbox.

    Идемпотентность обеспечивается ``dedupe_key`` записи outbox: повторный вызов
    с тем же ``message_id`` не создаст вторую отправку. Здесь ``message_id``
    новый на каждый запрос, поэтому повтор HTTP-запроса создаст новое сообщение —
    защита от дублей на этом уровне сознательно не делается: рекрутер вправе
    отправить два одинаковых текста подряд.
    """
    async with container.uow_factory() as uow:
        conversation = await uow.conversations.get_with_lock(conversation_id)
        if conversation is None:
            msg = f"Диалог {conversation_id} не найден"
            raise NotFound(msg)
        require_channel(container, conversation.channel)

        message = Message(
            id=new_id(),
            conversation_id=conversation.id,
            direction=MessageDirection.OUTBOUND,
            body=text,
            kind=MessageKind.MANUAL,
            generated_by_llm=False,
        )
        await uow.messages.add(message)

        entry = OutboxEntry(
            id=new_id(),
            topic=OUTBOX_TOPIC_SEND_MESSAGE,
            payload={
                "conversation_id": str(conversation.id),
                "message_id": str(message.id),
                "channel": conversation.channel.value,
                "conversation_external_id": conversation.external_id,
                "text": text,
                "requested_by": requested_by,
            },
            dedupe_key=f"manual-message:{message.id}",
        )
        await uow.outbox.add(entry)
        await uow.commit()

    # Запись уже надёжно лежит в БД: если очередь недоступна, её заберёт
    # cron-задача dispatch_outbox, поэтому ошибка постановки — только warning.
    try:
        await container.queue.enqueue(JOB_DISPATCH_OUTBOX, {})
    except Exception as exc:
        logger.warning(
            "manual_message_enqueue_failed",
            error_type=type(exc).__name__,
            outbox_entry_id=str(entry.id),
        )

    logger.info(
        "manual_message_queued",
        conversation_id=str(conversation_id),
        message_id=str(message.id),
        requested_by=requested_by,
    )
    return ManualMessageResult(message_id=message.id, outbox_entry_id=entry.id)


__all__ = [
    "OUTBOX_TOPIC_SEND_MESSAGE",
    "ManualMessageResult",
    "require_channel",
    "send_manual_message",
]
