"""Инциденты бронирования собеседования.

Сценарии взяты из ARCHITECTURE.md (п. 3.3 и 6.6) и раздела 6.3:

* ответ на создание встречи потерялся в таймауте — мы не знаем, создана она
  или нет; повторный шаг обязан найти уже созданную встречу по маркеру
  идемпотентности и **не создать вторую**;
* двойная бронь одного и того же слота — самая дорогая ошибка расписания:
  кандидат и интервьюер приходят на одно время «вдвоём»;
* занятость, появившаяся между подбором слота и созданием встречи.

Тесты проверяют наблюдаемый результат: сколько встреч реально создано в
календаре, сколько броней в БД и на какое время.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import timedelta

import pytest
from resilience_support import (
    BlindBusyCalendar,
    BusyAppearingOnCreate,
    TimeoutAfterCreate,
    add_outbox_entry,
    calendar_of,
    install_calendar,
    outbox_entry,
    seed_pipeline,
    store_of,
)

from hirebot.container import Container
from hirebot.domain.entities import new_id
from hirebot.domain.enums import ConversationState, OutboxStatus
from hirebot.domain.errors import ChannelUnavailable, SlotConflict
from hirebot.services.outbox import OutboxDispatcher
from hirebot.services.scheduling import SchedulingService
from hirebot.services.topics import BOOK_INTERVIEW_TOPIC


async def test_lost_create_response_is_recovered_by_idempotency_marker(container: Container) -> None:
    """Таймаут после создания встречи: повтор находит её и не создаёт вторую."""
    lossy_calendar = TimeoutAfterCreate(clock=container.clock)
    install_calendar(container, lossy_calendar)
    conversation_id = await seed_pipeline(container, state=ConversationState.SCREENED)
    service = SchedulingService(container)
    # Слот адресуем временем (``slot_start``): после созданной встречи индекс в
    # пересобранной сетке указывал бы уже на другое время, и повтор забронировал
    # бы второй слот вместо того, чтобы найти уже созданную встречу.
    target = (await service.propose_slots(conversation_id=conversation_id))[0]

    with pytest.raises(ChannelUnavailable):
        await service.book(conversation_id=conversation_id, slot_start=target.start)

    # Встреча в календаре всё-таки создана: именно поэтому повтор безопасен
    # только через маркер идемпотентности, а не через «создать ещё раз».
    assert lossy_calendar.lost_responses == 1
    assert len(lossy_calendar.meetings) == 1

    booking = await service.book(conversation_id=conversation_id, slot_start=target.start)

    store = await store_of(container)
    assert len(lossy_calendar.meetings) == 1, "повтор создал вторую встречу в календаре"
    assert len(store.bookings) == 1, "повтор создал вторую бронь в БД"
    assert booking.external_meeting_id in lossy_calendar.meetings
    assert booking.start == target.start, "повтор забронировал другое время"


async def test_repeated_booking_of_same_slot_returns_same_booking(container: Container) -> None:
    """Два последовательных ``book`` на один слот — одна бронь с тем же ``id``."""
    conversation_id = await seed_pipeline(container, state=ConversationState.SCREENED)
    service = SchedulingService(container)
    target = (await service.propose_slots(conversation_id=conversation_id))[0]

    first = await service.book(conversation_id=conversation_id, slot_start=target.start)
    second = await service.book(conversation_id=conversation_id, slot_start=target.start)

    store = await store_of(container)
    assert second.id == first.id
    assert len(store.bookings) == 1
    assert len(calendar_of(container).meetings) == 1


async def test_slot_index_still_addresses_current_grid(container: Container) -> None:
    """``slot_index`` (обратная совместимость) — индекс в текущей сетке.

    Новые задачи и API обязаны передавать ``slot_start``; индекс сохранён, чтобы
    ранее поставленные задачи продолжали разбираться, и означает ровно «индекс в
    сетке на момент вызова».
    """
    conversation_id = await seed_pipeline(container, state=ConversationState.SCREENED)
    service = SchedulingService(container)
    slots = await service.propose_slots(conversation_id=conversation_id)

    booking = await service.book(conversation_id=conversation_id, slot_index=1)

    assert booking.start == slots[1].start


async def test_outbox_book_interview_addresses_slot_by_time(container: Container) -> None:
    """Задача ``book_interview`` несёт время слота, а не только индекс.

    Индекс в payload устарел (после первой брони сетка сдвинулась): сервис обязан
    взять время из ``slot_start``. Повтор задачи возвращает ту же бронь и не
    создаёт вторую встречу на «следующее свободное» время — это и есть защита от
    двойной брони при повторном разборе outbox.
    """
    conversation_id = await seed_pipeline(container, state=ConversationState.SCREENED)
    service = SchedulingService(container)
    target = (await service.propose_slots(conversation_id=conversation_id))[0]
    payload = {
        "conversation_id": str(conversation_id),
        "slot_index": 0,  # устаревшая форма: после брони индекс укажет на другое время
        "slot_start": target.start.isoformat(),
        "requested_by": "worker",
    }
    first_id = await add_outbox_entry(container, topic=BOOK_INTERVIEW_TOPIC, payload=payload)
    dispatcher = OutboxDispatcher(container)

    assert await dispatcher.drain() == 1
    first = await outbox_entry(container, first_id)
    assert first.status is OutboxStatus.SENT

    # Повтор шага (воркер ушёл в рестарт): та же задача с другим ключом дедупликации.
    await add_outbox_entry(container, topic=BOOK_INTERVIEW_TOPIC, payload=payload, dedupe_key="book:replay")
    assert await dispatcher.drain() == 1

    store = await store_of(container)
    assert len(store.bookings) == 1, "повтор задачи создал вторую бронь"
    booking = next(iter(store.bookings.values()))
    assert booking.start == target.start, "задача забронировала другое время"
    assert len(calendar_of(container).meetings) == 1, "повтор создал вторую встречу"


async def test_slot_taken_after_first_booking_is_not_silently_rebooked(container: Container) -> None:
    """Занятое после первой брони время — это ``SlotConflict``, а не вторая встреча."""
    calendar = calendar_of(container)
    conversation_id = await seed_pipeline(container, state=ConversationState.SCREENED)
    service = SchedulingService(container)
    target = (await service.propose_slots(conversation_id=conversation_id))[0]
    first = await service.book(conversation_id=conversation_id, slot_start=target.start)

    # Время заняли: кандидат или интервьюер уже не свободны в этот час.
    calendar.add_busy_range(first.start, first.end)

    with pytest.raises(SlotConflict):
        await service.book(conversation_id=conversation_id, slot_start=first.start)

    assert len(calendar.meetings) == 1, "сервис молча создал вторую встречу"


async def test_busy_appearing_between_slot_choice_and_create_blocks_booking(container: Container) -> None:
    """Гонка: занятость появилась после подбора слота — двойной брони быть не должно.

    Календарь (как и Talantix) конфликты не валидирует, поэтому занятость,
    появившаяся между подбором слота и созданием встречи, обязана приводить к
    ``SlotConflict``, а созданная «поверх занятости» встреча — отменяться. Это и
    есть наблюдаемый результат: после блокировки брони в календаре не остаётся
    встреч, пересекающихся с чужой занятостью.
    """
    racing_calendar = BusyAppearingOnCreate(clock=container.clock)
    install_calendar(container, racing_calendar)
    conversation_id = await seed_pipeline(container, state=ConversationState.SCREENED)

    with pytest.raises(SlotConflict):
        await SchedulingService(container).book(conversation_id=conversation_id, slot_index=0)

    created = list(racing_calendar.meetings.values())
    overlapping = [
        meeting
        for meeting in created
        for race in racing_calendar.races
        if meeting.start < race.end and race.start < meeting.end
    ]
    assert not overlapping, "создана встреча поверх занятости, появившейся после подбора слота"


async def test_two_candidates_racing_for_one_slot_are_stopped_by_storage(
    container: Container,
) -> None:
    """Гонка двух кандидатов на одно время: второй получает ``SlotConflict``.

    Календарь намеренно не сообщает занятость (``BlindBusyCalendar``): площадка
    не валидирует конфликты и может не отдавать занятость вовсе. Тогда обе
    проверки календаря бессильны, и работать обязано ограничение хранилища —
    две активные брони одного менеджера на одно время невозможны. Лишняя
    встреча при этом отменяется, чтобы в календаре не осталось двойной брони.
    """
    blind_calendar = BlindBusyCalendar(clock=container.clock)
    install_calendar(container, blind_calendar)
    first_conversation = await seed_pipeline(container, state=ConversationState.SCREENED)
    second_conversation = await seed_pipeline(container, state=ConversationState.SCREENED, suffix="2")

    first = await SchedulingService(container).book(conversation_id=first_conversation, slot_index=0)

    second_service = SchedulingService(container)
    second_slot = (await second_service.propose_slots(conversation_id=second_conversation))[0]

    with pytest.raises(SlotConflict):
        await second_service.book(conversation_id=second_conversation, slot_start=second_slot.start)

    store = await store_of(container)
    assert first.start == second_slot.start, "сценарий должен быть про одно и то же время"
    assert len(store.bookings) == 1, "создана вторая активная бронь на то же время"
    assert len(blind_calendar.meetings) == 1, "лишняя встреча осталась в календаре"

    # Ограничение хранилища действует и в обход сервиса: оно повторяет частичный
    # уникальный индекс БД, а не только проверку занятости в сервисе.
    async with container.uow_factory() as uow:
        with pytest.raises(SlotConflict):
            await uow.bookings.save(replace(first, id=new_id(), idempotency_key=""))


async def test_propose_slots_never_offers_past_time(container: Container) -> None:
    """Прошедшее время не предлагается: ни календарём, ни сервисом.

    Окно подбора начинается с начала текущего часа, поэтому в него попадает
    слот, который уже начался. Календарь такие слоты не отдаёт (контракт порта),
    сервис дублирует фильтр — наблюдаемый результат один: предложений в прошлом
    нет, хотя свободное время в будущем есть.
    """
    calendar = calendar_of(container)
    conversation_id = await seed_pipeline(container, state=ConversationState.SCREENED)
    # 12:30 UTC: окно начинается с 12:00 — этот слот уже в прошлом.
    container.clock.set(container.clock.now() + timedelta(hours=3, minutes=30))
    now = container.clock.now()

    unfiltered = await calendar.find_free_slots(
        manager_ids=[],
        start=now.replace(minute=0, second=0, microsecond=0),
        end=now + timedelta(days=1),
        duration_minutes=60,
        limit=3,
    )
    assert all(slot.start > now for slot in unfiltered), "календарь предложил прошедшее время"

    slots = await SchedulingService(container).propose_slots(conversation_id=conversation_id)

    assert slots, "свободное время в будущем есть — список не должен быть пустым"
    assert all(slot.start > now for slot in slots), "предложено прошедшее время"
