"""Инцидент дублирования сообщения при повторе шага воронки.

Два разных повода повторить шаг:

* воркер вызвал ``process`` второй раз на том же диалоге (повторная задача,
  тик поллера, ручной прогон) — шаг обязан увидеть изменённое состояние и
  ничего не отправить;
* сообщение уже ушло в чат площадки, а коммит в БД потерялся (падение между
  отправкой и фиксацией). Тогда состояние снова ``NEW``, шаг выполняется
  заново, и от дубля спасает только ``idempotency_key`` (ARCHITECTURE.md, п. 2.4).

Проверяется наблюдаемый результат: сколько сообщений реально получил кандидат
(``texts_for``) и сколько исходящих сообщений лежит в БД.
"""

from __future__ import annotations

from resilience_support import (
    CONVERSATION_EXTERNAL_ID,
    channel_of,
    rewind_last_step,
    seed_pipeline,
    stored_messages,
)

from hirebot.container import Container
from hirebot.domain.enums import ConversationState, MessageDirection
from hirebot.services.conversation import ConversationService


async def test_repeated_funnel_step_sends_nothing_new(container: Container) -> None:
    """Повторный ``process`` на том же диалоге не отправляет сообщение снова."""
    conversation_id = await seed_pipeline(container)
    service = ConversationService(container)
    channel = channel_of(container)

    first = await service.process(conversation_id)
    delivered = channel.texts_for(CONVERSATION_EXTERNAL_ID)
    assert first.action_taken != "noop"
    assert len(delivered) == 1

    second = await service.process(conversation_id)

    assert second.action_taken == "noop"
    assert channel.texts_for(CONVERSATION_EXTERNAL_ID) == delivered


async def test_retry_after_lost_commit_does_not_duplicate_the_message(container: Container) -> None:
    """Повтор шага после потерянного коммита: кандидат получает одно сообщение."""
    conversation_id = await seed_pipeline(container)
    service = ConversationService(container)
    channel = channel_of(container)

    first = await service.process(conversation_id)
    delivered = channel.texts_for(CONVERSATION_EXTERNAL_ID)
    assert len(delivered) == 1

    # Сообщение ушло в чат, но последняя транзакция шага не зафиксировалась: в БД
    # нет ни сообщения, ни перехода в QUESTIONS_SENT/INVITED. Диалог остался на
    # вехе SCREENED (она коммитится до отправки), воркер повторит шаг.
    await rewind_last_step(container, conversation_id, state=ConversationState.SCREENED)

    outcome = await service.process(conversation_id)

    assert channel.texts_for(CONVERSATION_EXTERNAL_ID) == delivered, "кандидат получил дубль"
    assert outcome.action_taken == first.action_taken
    assert outcome.state is first.state

    messages = await stored_messages(container, conversation_id)
    outbound = [message for message in messages if message.direction is MessageDirection.OUTBOUND]
    assert len(outbound) == 1, "в БД оказались два одинаковых исходящих сообщения"
