"""Инцидент повторной доставки вебхука.

Площадки доставляют уведомления по принципу at-least-once: при таймауте ответа
приходит тот же самый вебхук второй раз. Ожидаемое поведение
(``api/routes_webhooks.py``): повтор отбрасывается по ключу события, воронка
запускается один раз, диалог и задача обработки создаются ровно одни.

Здесь проверяется сквозной результат: сколько диалогов и сколько записей
outbox появилось после двух доставок одного уведомления.
"""

from __future__ import annotations

from resilience_support import publish_incoming, store_of

from hirebot.api.routes_webhooks import claim_event
from hirebot.container import Container
from hirebot.domain.enums import Channel
from hirebot.infra.cache import MemoryCache
from hirebot.services.ingest import IngestService

EVENT_KEY = "hh-event-42"


async def test_replayed_webhook_produces_one_conversation_and_one_processing(
    container: Container,
) -> None:
    """Две доставки одного уведомления: один диалог и одна задача обработки."""
    # Уведомление пришло: ставим событие «в обработку» и поллим площадку.
    container.cache = MemoryCache(clock=container.clock)
    response = await publish_incoming(container)
    ingest = IngestService(container)

    assert await claim_event(container, Channel.HH, EVENT_KEY) is True
    first = await ingest.poll(Channel.HH)

    # Площадка не дождалась ответа и повторила доставку того же события.
    assert await claim_event(container, Channel.HH, EVENT_KEY) is False
    repeated = await ingest.poll(Channel.HH)

    store = await store_of(container)
    assert first.created == 1
    assert repeated.found == 0, "повторный поллинг снова увидел тот же отклик"
    assert len(store.conversations) == 1
    assert len(store.outbox) == 1

    # Прямой повтор приёма того же отклика тоже ничего не создаёт: защита
    # работает и без кэша событий, на уровне (channel, external_id).
    outcome = await ingest.ingest(response)

    store = await store_of(container)
    assert outcome.created is False
    assert len(store.conversations) == 1
    assert len(store.outbox) == 1


async def test_replayed_webhook_is_rejected_by_container_cache(container: Container) -> None:
    """Кэш контейнера обязан отсекать повторное событие, а не пропускать его."""
    await publish_incoming(container)

    assert await claim_event(container, Channel.HH, EVENT_KEY) is True
    assert await claim_event(container, Channel.HH, EVENT_KEY) is False
