"""Инциденты outbox: ретраи, backoff и «мёртвые» задачи.

Outbox — механизм, который одновременно решает две проблемы (ARCHITECTURE.md,
п. 2.3): «отправили в чат, упали до коммита» и «закоммитили, упали до отправки».
Обратная сторона — задача, которая падает всегда: она не должна ни теряться, ни
выполняться бесконечно, ни блокировать остальные.

Проверяется наблюдаемое состояние записи (``attempts``, ``status``,
``last_error``), отложенные задачи очереди и то, что успешный шаг не
выполняется повторно.
"""

from __future__ import annotations

from resilience_support import (
    add_outbox_entry,
    advance_clock,
    channel_of,
    make_container,
    make_settings,
    outbox_entry,
    queue_of,
)

from hirebot.config import QueueSettings
from hirebot.container import Container
from hirebot.domain.enums import OutboxStatus
from hirebot.services.outbox import OutboxDispatcher, backoff_delay_s
from hirebot.services.topics import (
    DISPATCH_OUTBOX_TOPIC,
    PROCESS_CONVERSATION_TOPIC,
    SEND_MESSAGE_TOPIC,
)

#: Потолок попыток в тестах: маленький, чтобы сценарий читался.
MAX_ATTEMPTS = 3

CONVERSATION_EXTERNAL_ID = "resilience-chat-1"


def make_retry_container() -> Container:
    """Контейнер с явным ``queue.max_attempts`` (иначе потолок не проверить)."""
    return make_container(settings=make_settings(queue=QueueSettings(max_attempts=MAX_ATTEMPTS)))


async def test_task_dies_after_max_attempts_and_is_never_claimed_again() -> None:
    """Задача, падающая всегда, становится ``DEAD`` и больше не берётся."""
    container = make_retry_container()
    # Пустой payload гарантированно ломает обработчик: это «неисправимая» задача.
    entry_id = await add_outbox_entry(container, topic=PROCESS_CONVERSATION_TOPIC, payload={})
    dispatcher = OutboxDispatcher(container)

    for expected_attempts in range(1, MAX_ATTEMPTS + 1):
        assert await dispatcher.drain() == 0
        entry = await outbox_entry(container, entry_id)
        assert entry.attempts == expected_attempts
        # Пауза между повторами — свойство хранилища: пока время не сдвинулось,
        # запись снова не заберут (иначе backoff обходится любым ``drain``).
        advance_clock(container, backoff_delay_s(expected_attempts))

    dead = await outbox_entry(container, entry_id)
    assert dead.status is OutboxStatus.DEAD
    assert dead.last_error
    assert "ValueError" in dead.last_error

    assert await dispatcher.drain() == 0
    after = await outbox_entry(container, entry_id)
    assert after.status is OutboxStatus.DEAD
    assert after.attempts == MAX_ATTEMPTS, "мёртвую задачу снова взяли в работу"


async def test_each_failure_schedules_longer_delayed_retry() -> None:
    """Backoff растёт экспоненциально, а после ``DEAD`` повторов не планируется."""
    container = make_retry_container()
    await add_outbox_entry(container, topic=PROCESS_CONVERSATION_TOPIC, payload={})
    dispatcher = OutboxDispatcher(container)

    await dispatcher.drain()
    advance_clock(container, backoff_delay_s(1))
    await dispatcher.drain()

    delays = [job.delay_s for job in queue_of(container).of(DISPATCH_OUTBOX_TOPIC)]
    assert delays == [backoff_delay_s(1), backoff_delay_s(2)]
    assert delays[0] < delays[1], "пауза между повторами не растёт"

    advance_clock(container, backoff_delay_s(2))
    await dispatcher.drain()  # третья попытка -> DEAD: повтор больше не планируется
    assert len(queue_of(container).of(DISPATCH_OUTBOX_TOPIC)) == 2


async def test_failed_task_is_not_retried_before_backoff_elapses() -> None:
    """Повтор не должен выполняться раньше выдержанной паузы.

    Пауза живёт в самом хранилище (``available_at``), а не только в отложенной
    задаче очереди: произвольный ``drain`` не может её обойти.
    """
    container = make_retry_container()
    entry_id = await add_outbox_entry(container, topic=PROCESS_CONVERSATION_TOPIC, payload={})
    dispatcher = OutboxDispatcher(container)

    await dispatcher.drain()  # попытка 1: FAILED, повтор отложен на backoff_delay_s(1)
    await dispatcher.drain()  # без ожидания паузы

    entry = await outbox_entry(container, entry_id)
    assert entry.attempts == 1, "повтор выполнен раньше экспоненциальной паузы"
    assert entry.available_at > container.clock.now(), "пауза не отложена в хранилище"


async def test_successful_task_is_marked_sent_and_not_executed_twice(container: Container) -> None:
    """Успешная задача помечается ``SENT`` и не выполняется при следующем ``drain``."""
    channel = channel_of(container)
    entry_id = await add_outbox_entry(
        container,
        topic=SEND_MESSAGE_TOPIC,
        payload={
            "channel": "hh",
            "conversation_external_id": CONVERSATION_EXTERNAL_ID,
            "idempotency_key": "outbox-msg-1",
            "text": "Здравствуйте! Спасибо за отклик.",
        },
    )
    dispatcher = OutboxDispatcher(container)

    assert await dispatcher.drain() == 1

    sent = await outbox_entry(container, entry_id)
    assert sent.status is OutboxStatus.SENT
    assert channel.texts_for(CONVERSATION_EXTERNAL_ID) == ["Здравствуйте! Спасибо за отклик."]

    assert await dispatcher.drain() == 0
    assert channel.texts_for(CONVERSATION_EXTERNAL_ID) == ["Здравствуйте! Спасибо за отклик."]


async def test_replayed_send_step_does_not_duplicate_the_message(container: Container) -> None:
    """Шаг отправки, поставленный заново после потери коммита, не дублирует сообщение.

    Ключ идемпотентности — единственная защита от дубля: воркер мог уйти в
    рестарт между отправкой и фиксацией, и задача будет поставлена снова.
    """
    channel = channel_of(container)
    payload = {
        "channel": "hh",
        "conversation_external_id": CONVERSATION_EXTERNAL_ID,
        "idempotency_key": "outbox-msg-2",
        "text": "Готово! Записал вас на собеседование.",
    }
    await add_outbox_entry(container, topic=SEND_MESSAGE_TOPIC, payload=payload, dedupe_key="send:1")
    dispatcher = OutboxDispatcher(container)
    assert await dispatcher.drain() == 1

    await add_outbox_entry(container, topic=SEND_MESSAGE_TOPIC, payload=payload, dedupe_key="send:1:replay")
    assert await dispatcher.drain() == 1

    assert channel.texts_for(CONVERSATION_EXTERNAL_ID) == ["Готово! Записал вас на собеседование."]
