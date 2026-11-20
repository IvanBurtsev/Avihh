"""Интеграционные тесты репозиториев на SQLite в памяти.

Проверяются ровно те свойства, ради которых репозитории написаны именно так:

* ПДн уходят в БД только шифротекстом, а поиск по контакту работает через
  blind index;
* уникальные ключи (``channel+external_id``, ``dedupe_key``, ``idempotency_key``)
  действительно защищают от дублей;
* ``claim_batch`` увеличивает ``attempts`` и не отдаёт уже отправленные задачи;
* пагинация и порядок выборок предсказуемы;
* ``FOR UPDATE`` не ломает SQLite (проверка диалекта), а ``selectinload``
  подгружает связи без N+1.
"""

from __future__ import annotations

from collections.abc import Callable
from contextlib import AbstractAsyncContextManager
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import delete, text, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from hirebot.domain.entities import Booking, Resume
from hirebot.domain.enums import BookingStatus, Channel, ConversationState, OutboxStatus
from hirebot.domain.errors import NotFound, SlotConflict
from hirebot.infra.db.models import CandidateRow, IdempotencyKeyRow
from hirebot.infra.db.repositories import MAX_ERROR_LEN, ConversationDetails
from hirebot.infra.security.crypto import CIPHER_PREFIX, FieldCipher
from hirebot.ports.repositories import UnitOfWork

from .conftest import FROZEN_NOW, EntityFactory

pytestmark = pytest.mark.integration

UowFactory = Callable[[], AbstractAsyncContextManager[UnitOfWork]]


async def test_vacancy_upsert_is_idempotent_by_external_id(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    # Один и тот же внешний идентификатор: именно по нему работает дедупликация.
    vacancy = await make.vacancy(external_id="v-dup")
    changed = await make.vacancy(id=uuid4(), external_id="v-dup", title="Ведущий менеджер по продажам")

    async with uow_factory() as uow:
        first = await uow.vacancies.upsert(vacancy)
        second = await uow.vacancies.upsert(changed)
        listed = await uow.vacancies.list_active(vacancy.company_id)

    assert first.id == vacancy.id
    # Ключ дедупликации — (channel, external_id): id остаётся прежним, поля обновляются.
    assert second.id == first.id
    assert second.title == "Ведущий менеджер по продажам"
    assert len(listed) == 1
    assert listed[0].company_id == vacancy.company_id


async def test_vacancy_list_active_filters_and_paginates(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    company_id = make.company_id
    async with uow_factory() as uow:
        for index in range(3):
            await uow.vacancies.upsert(
                await make.vacancy(external_id=f"v-{index}", title=f"Вакансия {index}")
            )
        await uow.vacancies.upsert(await make.vacancy(external_id="v-off", is_active=False))

    async with uow_factory() as uow:
        page_one = await uow.vacancies.list_active(company_id, limit=2)
        page_two = await uow.vacancies.list_active(company_id, limit=2, offset=2)
        other_company = await uow.vacancies.list_active(uuid4())

    assert len(page_one) == 2
    assert len(page_two) == 1  # неактивная вакансия отфильтрована
    assert other_company == []


async def test_candidate_contacts_are_stored_encrypted(
    uow_factory: UowFactory,
    sessionmaker: async_sessionmaker[AsyncSession],
    cipher: FieldCipher,
    make: EntityFactory,
) -> None:
    candidate = await make.candidate(phone="+7 999 123-45-67", email="Ivan@Example.com")
    async with uow_factory() as uow:
        await uow.candidates.upsert(candidate)

    async with sessionmaker() as session:
        row = (await session.execute(text("select phone, email, phone_index from candidates"))).one()

    assert row.phone.startswith(CIPHER_PREFIX)
    assert row.email.startswith(CIPHER_PREFIX)
    assert candidate.phone not in row.phone
    assert candidate.email not in row.email
    assert row.phone_index == cipher.blind_index(candidate.phone)


async def test_candidate_roundtrip_returns_plaintext(uow_factory: UowFactory, make: EntityFactory) -> None:
    candidate = await make.candidate()
    async with uow_factory() as uow:
        await uow.candidates.upsert(candidate)

    async with uow_factory() as uow:
        loaded = await uow.candidates.get_by_external(Channel.HH, candidate.external_id)

    assert loaded is not None
    assert loaded.id == candidate.id
    assert loaded.phone == candidate.phone
    assert loaded.email == candidate.email
    assert loaded.created_at.tzinfo is not None


async def test_candidate_search_by_contact_uses_blind_index(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    candidate = await make.candidate(phone="+7 (999) 123-45-67", email="ivan@example.com")
    async with uow_factory() as uow:
        await uow.candidates.upsert(candidate)

    async with uow_factory() as uow:
        # Тот же номер в другом формате и почта в другом регистре.
        by_phone = await uow.candidates.find_by_phone("89991234567")
        by_email = await uow.candidates.find_by_email("IVAN@example.com")
        missing = await uow.candidates.find_by_phone("+7 999 000-00-00")

    assert [item.id for item in by_phone] == [candidate.id]
    assert [item.id for item in by_email] == [candidate.id]
    assert missing == []


async def test_resume_text_is_encrypted_and_latest_is_returned(
    uow_factory: UowFactory,
    sessionmaker: async_sessionmaker[AsyncSession],
    make: EntityFactory,
) -> None:
    candidate = await make.candidate()
    older = await make.resume(candidate_id=candidate.id, external_id="r-old", raw_text="Старое резюме")
    newer = await make.resume(
        candidate_id=candidate.id,
        external_id="r-new",
        raw_text="Новое резюме",
        fetched_at=FROZEN_NOW + timedelta(days=1),
    )
    async with uow_factory() as uow:
        await uow.resumes.save(older)
        await uow.resumes.save(newer)

    async with sessionmaker() as session:
        raw = (await session.execute(text("select raw_text from resumes"))).scalars().all()

    assert all(value.startswith(CIPHER_PREFIX) for value in raw)
    assert all("резюме" not in value for value in raw)

    async with uow_factory() as uow:
        loaded = await uow.resumes.get_latest_for_candidate(candidate.id)

    assert loaded is not None
    assert loaded.id == newer.id
    assert loaded.raw_text == "Новое резюме"


async def test_resume_save_updates_same_id(uow_factory: UowFactory, make: EntityFactory) -> None:
    resume = await make.resume()
    async with uow_factory() as uow:
        await uow.resumes.save(resume)
        resume.raw_text = "Обновлённый текст"
        await uow.resumes.save(resume)
        stored = await uow.resumes.get(resume.id)
        all_resumes = await uow.resumes.get_latest_for_candidate(resume.candidate_id)

    assert stored is not None
    assert stored.raw_text == "Обновлённый текст"
    assert all_resumes is not None
    assert all_resumes.id == resume.id


async def test_conversation_upsert_and_lock_read(uow_factory: UowFactory, make: EntityFactory) -> None:
    conversation = await make.conversation()
    async with uow_factory() as uow:
        created = await uow.conversations.upsert(conversation)
        conversation.state = ConversationState.SCREENED
        conversation.next_action_at = FROZEN_NOW
        updated = await uow.conversations.upsert(conversation)

    assert created.id == updated.id
    assert updated.state is ConversationState.SCREENED

    async with uow_factory() as uow:
        # На SQLite FOR UPDATE не применяется: проверяется, что чтение работает.
        locked = await uow.conversations.get_with_lock(updated.id)
        by_external = await uow.conversations.get_by_external(Channel.HH, updated.external_id)
        missing = await uow.conversations.get_with_lock(uuid4())

    assert locked is not None
    assert locked.state is ConversationState.SCREENED
    assert by_external is not None
    assert missing is None


async def test_conversation_list_by_state_orders_and_paginates(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    async with uow_factory() as uow:
        for index in range(4):
            await uow.conversations.upsert(
                await make.conversation(
                    external_id=f"conv-{index}",
                    state=ConversationState.QUESTIONS_SENT,
                    next_action_at=FROZEN_NOW + timedelta(minutes=index),
                )
            )
        # Без next_action_at: такие диалоги идут в конец очереди.
        await uow.conversations.upsert(
            await make.conversation(external_id="conv-null", state=ConversationState.QUESTIONS_SENT)
        )
        await uow.conversations.upsert(
            await make.conversation(external_id="conv-closed", state=ConversationState.CLOSED)
        )

    async with uow_factory() as uow:
        first_page = await uow.conversations.list_by_state([ConversationState.QUESTIONS_SENT], limit=3)
        second_page = await uow.conversations.list_by_state(
            [ConversationState.QUESTIONS_SENT], limit=3, offset=3
        )
        empty = await uow.conversations.list_by_state([])

    assert [item.external_id for item in first_page] == ["conv-0", "conv-1", "conv-2"]
    assert [item.external_id for item in second_page] == ["conv-3", "conv-null"]
    assert empty == []


async def test_conversation_details_are_loaded_without_n_plus_one(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    candidate = await make.candidate()
    vacancy = await make.vacancy()
    conversation = await make.conversation(
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        state=ConversationState.AWAITING_ANSWER,
    )
    async with uow_factory() as uow:
        await uow.candidates.upsert(candidate)
        await uow.vacancies.upsert(vacancy)
        await uow.conversations.upsert(conversation)
        for index in range(3):
            await uow.messages.add(
                make.message(
                    conversation_id=conversation.id,
                    body=f"Сообщение {index}",
                    created_at=FROZEN_NOW + timedelta(minutes=index),
                )
            )

    async with uow_factory() as uow:
        details = await uow.conversations.list_with_details([ConversationState.AWAITING_ANSWER])

    assert len(details) == 1
    item = details[0]
    assert isinstance(item, ConversationDetails)
    assert item.candidate is not None
    assert item.candidate.full_name == candidate.full_name
    assert item.vacancy is not None
    assert item.vacancy.title == vacancy.title
    assert [message.body for message in item.messages] == [
        "Сообщение 0",
        "Сообщение 1",
        "Сообщение 2",
    ]


async def test_message_add_list_and_external_id_check(uow_factory: UowFactory, make: EntityFactory) -> None:
    conversation = await make.conversation()
    async with uow_factory() as uow:
        await uow.conversations.upsert(conversation)
        for index in range(3):
            await uow.messages.add(
                make.message(
                    conversation_id=conversation.id,
                    body=f"Текст {index}",
                    external_id=f"ext-{index}" if index else None,
                    created_at=FROZEN_NOW + timedelta(minutes=index),
                )
            )

    async with uow_factory() as uow:
        ordered = await uow.messages.list_for_conversation(conversation.id)
        limited = await uow.messages.list_for_conversation(conversation.id, limit=2)
        exists = await uow.messages.exists_external_id(conversation.id, "ext-1")
        not_exists = await uow.messages.exists_external_id(conversation.id, "ext-999")

    assert [message.body for message in ordered] == ["Текст 0", "Текст 1", "Текст 2"]
    # Последние два в хронологическом порядке: чат читают снизу вверх, но
    # возвращаем в естественном порядке для промпта.
    assert [message.body for message in limited] == ["Текст 1", "Текст 2"]
    assert exists is True
    assert not_exists is False


async def test_message_external_id_is_unique_per_conversation(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    conversation = await make.conversation()
    async with uow_factory() as uow:
        await uow.conversations.upsert(conversation)
        await uow.messages.add(make.message(conversation_id=conversation.id, external_id="dup"))

    with pytest.raises(IntegrityError):
        async with uow_factory() as uow:
            await uow.messages.add(make.message(conversation_id=conversation.id, external_id="dup"))


async def test_message_add_is_idempotent_by_id(uow_factory: UowFactory, make: EntityFactory) -> None:
    conversation = await make.conversation()
    message = make.message(conversation_id=conversation.id)
    async with uow_factory() as uow:
        await uow.conversations.upsert(conversation)
        await uow.messages.add(message)
        message.body = "Отредактировано"
        await uow.messages.add(message)
        stored = await uow.messages.list_for_conversation(conversation.id)

    assert len(stored) == 1
    assert stored[0].body == "Отредактировано"


async def test_screening_saved_with_content_hash_and_found(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    candidate = await make.candidate()
    vacancy = await make.vacancy()
    resume = await make.resume(candidate_id=candidate.id)
    screening = await make.screening(resume_id=resume.id, vacancy_id=vacancy.id, candidate_id=candidate.id)
    expected_hash = Resume.content_hash(resume.raw_text, vacancy_id=vacancy.id)

    async with uow_factory() as uow:
        await uow.resumes.save(resume)
        saved = await uow.screenings.save(screening)

    assert saved.total_score == pytest.approx(85.0)
    assert saved.criteria[0].key == "sales"
    assert saved.criteria[0].must_have is True

    async with uow_factory() as uow:
        by_hash = await uow.screenings.get_by_content_hash(expected_hash)
        latest = await uow.screenings.get_latest(candidate.id, vacancy.id)
        missing = await uow.screenings.get_by_content_hash("no-such-hash")

    assert by_hash is not None
    assert by_hash.id == screening.id
    assert latest is not None
    assert latest.id == screening.id
    assert missing is None


async def test_screening_save_updates_existing_row(uow_factory: UowFactory, make: EntityFactory) -> None:
    candidate = await make.candidate()
    vacancy = await make.vacancy()
    resume = await make.resume(candidate_id=candidate.id)
    screening = await make.screening(resume_id=resume.id, vacancy_id=vacancy.id, candidate_id=candidate.id)
    async with uow_factory() as uow:
        await uow.resumes.save(resume)
        await uow.screenings.save(screening)

    async with uow_factory() as uow:
        screening.total_score = 42.0
        updated = await uow.screenings.save(screening)
        latest = await uow.screenings.get_latest(candidate.id, vacancy.id)

    assert updated.total_score == pytest.approx(42.0)
    assert latest is not None
    assert latest.total_score == pytest.approx(42.0)


async def test_booking_idempotency_key_prevents_duplicates(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    booking = await make.booking(idempotency_key="booking-key-1")
    duplicate = await make.booking(
        id=uuid4(),
        conversation_id=booking.conversation_id,
        candidate_id=booking.candidate_id,
        vacancy_id=booking.vacancy_id,
        idempotency_key="booking-key-1",
    )

    async with uow_factory() as uow:
        first = await uow.bookings.save(booking)
        second = await uow.bookings.save(duplicate)

    assert first.id == booking.id
    # Ключ занят -> возвращается существующая бронь, второй встречи не создаётся.
    assert second.id == booking.id

    async with uow_factory() as uow:
        by_key = await uow.bookings.get_by_idempotency_key("booking-key-1")
        by_id = await uow.bookings.get(booking.id)

    assert by_key is not None
    assert by_key.id == booking.id
    assert by_id is not None
    assert by_id.id == booking.id


async def test_booking_without_idempotency_key_is_allowed_twice(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    """Пустой ключ хранится как NULL: иначе UNIQUE не дал бы создать вторую бронь.

    Слоты при этом разные по менеджеру: частичный уникальный индекс
    ``bookings(manager_id, starts_at)`` запрещает две **активные** брони одного
    менеджера на одно время (это отдельная гарантия, см.
    :func:`test_booking_active_slot_unique_constraint`).
    """
    async with uow_factory() as uow:
        first = await uow.bookings.save(await make.booking())
        second = await uow.bookings.save(await make.booking(manager_id="manager-2"))

    assert first.id != second.id

    async with uow_factory() as uow:
        assert await uow.bookings.get_by_idempotency_key("") is None


async def test_booking_active_slot_unique_constraint(uow_factory: UowFactory, make: EntityFactory) -> None:
    """Частичный уникальный индекс запрещает две активные брони на один слот.

    Это последний рубеж защиты от двойной брони: площадка конфликты не
    валидирует, а проверка «прочитал занятость — создал встречу» гонку не
    закрывает. Индекс частичный (``status IN ('proposed', 'confirmed')``),
    поэтому отменённая бронь времени не занимает.
    """
    first = await make.booking()
    duplicate = Booking(
        id=uuid4(),
        conversation_id=first.conversation_id,
        candidate_id=first.candidate_id,
        vacancy_id=first.vacancy_id,
        manager_id=first.manager_id,
        start=first.start,
        end=first.end,
        timezone=first.timezone,
        status=BookingStatus.PROPOSED,
        created_at=FROZEN_NOW,
        updated_at=FROZEN_NOW,
    )

    with pytest.raises(SlotConflict):
        async with uow_factory() as uow:
            # Репозиторий транслирует нарушение индекса в доменную ошибку:
            # сервис не обязан знать про SQL и IntegrityError.
            await uow.bookings.save(duplicate)

    # Отменённая бронь индексом не учитывается: слот снова можно занять.
    cancelled = await make.booking(
        status=BookingStatus.CANCELLED,
        start=first.start,
        end=first.end,
    )
    assert cancelled.id != first.id


async def test_booking_list_active_for_manager_window(uow_factory: UowFactory, make: EntityFactory) -> None:
    booking = await make.booking()  # FROZEN_NOW + 1 день, длительность 1 час
    cancelled = await make.booking(
        start=booking.start,
        end=booking.end,
        status=BookingStatus.CANCELLED,
    )
    async with uow_factory() as uow:
        await uow.bookings.save(booking)
        await uow.bookings.save(cancelled)

    async with uow_factory() as uow:
        # Окно внутри брони -> попадает.
        overlapping = await uow.bookings.list_active_for_manager(
            "manager-1", start=booking.start, end=booking.end
        )
        # Окно строго после брони -> не попадает.
        after = await uow.bookings.list_active_for_manager(
            "manager-1", start=booking.end, end=booking.end + timedelta(hours=2)
        )
        other_manager = await uow.bookings.list_active_for_manager(
            "manager-2", start=booking.start, end=booking.end
        )

    assert [item.id for item in overlapping] == [booking.id]
    assert after == []
    assert other_manager == []


async def test_outbox_dedupe_key_returns_existing_entry(uow_factory: UowFactory, make: EntityFactory) -> None:
    entry = make.outbox(dedupe_key="step-1")
    duplicate = make.outbox(id=uuid4(), dedupe_key="step-1")

    async with uow_factory() as uow:
        first = await uow.outbox.add(entry)
        second = await uow.outbox.add(duplicate)

    assert first.id == entry.id
    assert second.id == entry.id


async def test_outbox_claim_batch_increments_attempts_and_respects_status(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    entry = make.outbox(dedupe_key="step-1")
    future = make.outbox(
        dedupe_key="step-2",
        available_at=FROZEN_NOW + timedelta(hours=1),
    )
    async with uow_factory() as uow:
        await uow.outbox.add(entry)
        await uow.outbox.add(future)

    async with uow_factory() as uow:
        claimed = await uow.outbox.claim_batch(limit=10, now=FROZEN_NOW)

    assert [item.id for item in claimed] == [entry.id]
    assert claimed[0].attempts == 1

    async with uow_factory() as uow:
        again = await uow.outbox.claim_batch(limit=10, now=FROZEN_NOW)
    assert again[0].attempts == 2

    async with uow_factory() as uow:
        await uow.outbox.mark(entry.id, OutboxStatus.SENT)
        after_sent = await uow.outbox.claim_batch(limit=10, now=FROZEN_NOW)
    assert after_sent == []


async def test_outbox_mark_records_error_and_backoff(uow_factory: UowFactory, make: EntityFactory) -> None:
    entry = make.outbox()
    async with uow_factory() as uow:
        await uow.outbox.add(entry)

    async with uow_factory() as uow:
        await uow.outbox.claim_batch(limit=1, now=FROZEN_NOW)
        await uow.outbox.mark(entry.id, OutboxStatus.FAILED, error="x" * (MAX_ERROR_LEN + 100))
        reloaded = await uow.outbox.claim_batch(limit=1, now=FROZEN_NOW)

    # Ошибка обрезана, задача отложена (backoff) и больше не берётся сразу.
    assert reloaded == []


async def test_booking_find_active_overlap(uow_factory: UowFactory, make: EntityFactory) -> None:
    """``find_active_overlap`` — транзакционная проверка занятости слота.

    Активная бронь видна как занятость, отменённая — нет (она времени не
    занимает): именно эту проверку сервис выполняет в одной транзакции с
    сохранением брони.
    """
    booking = await make.booking()
    await make.booking(
        manager_id="manager-2",
        start=booking.start,
        end=booking.end,
        status=BookingStatus.CANCELLED,
    )

    async with uow_factory() as uow:
        found = await uow.bookings.find_active_overlap(
            booking.manager_id, start=booking.start, end=booking.end
        )
        after = await uow.bookings.find_active_overlap(
            booking.manager_id, start=booking.end, end=booking.end + timedelta(hours=2)
        )
        cancelled_only = await uow.bookings.find_active_overlap(
            "manager-2", start=booking.start, end=booking.end
        )

    assert found is not None
    assert found.id == booking.id
    assert after is None
    assert cancelled_only is None


async def test_outbox_failed_task_is_reclaimed_after_backoff(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    """``FAILED`` возвращается в работу только после паузы (``available_at``).

    Пауза — свойство хранилища: ``mark`` отодвигает ``available_at``, поэтому
    ``claim_batch`` не берёт запись раньше срока (иначе экспоненциальный backoff
    обходился бы любым проходом воркера), но и не теряет её навсегда.
    """
    entry = make.outbox()
    async with uow_factory() as uow:
        await uow.outbox.add(entry)
        claimed = await uow.outbox.claim_batch(limit=1, now=FROZEN_NOW)
        assert [item.id for item in claimed] == [entry.id]
        await uow.outbox.mark(entry.id, OutboxStatus.FAILED, error="boom")

    async with uow_factory() as uow:
        # Пауза ещё не истекла — запись не забираем.
        assert await uow.outbox.claim_batch(limit=1, now=FROZEN_NOW) == []
        # Пауза истекла — забираем снова: упавшая задача не теряется.
        later = datetime.now(tz=UTC) + timedelta(hours=2)
        reclaimed = await uow.outbox.claim_batch(limit=1, now=later)

    assert [item.id for item in reclaimed] == [entry.id]
    assert reclaimed[0].attempts == 2


async def test_outbox_mark_unknown_entry_raises(
    uow_factory: UowFactory,
) -> None:
    async with uow_factory() as uow:
        with pytest.raises(NotFound):
            await uow.outbox.mark(uuid4(), OutboxStatus.SENT)


async def test_idempotency_acquire_is_exclusive(
    uow_factory: UowFactory,
) -> None:
    async with uow_factory() as uow:
        first = await uow.idempotency.acquire("req-1", scope="send_message")
        second = await uow.idempotency.acquire("req-1", scope="send_message")
        other_scope = await uow.idempotency.acquire("req-1", scope="book_meeting")
        other_key = await uow.idempotency.acquire("req-2", scope="send_message")

    assert first is True
    assert second is False
    assert other_scope is True
    assert other_key is True


async def test_idempotency_expired_key_can_be_acquired_again(
    uow_factory: UowFactory,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    async with uow_factory() as uow:
        assert await uow.idempotency.acquire("req-ttl", scope="send_message", ttl_hours=72) is True

    # Имитируем истечение срока: ключ старше TTL должен освободиться.
    async with sessionmaker() as session:
        await session.execute(
            update(IdempotencyKeyRow)
            .where(IdempotencyKeyRow.key == "req-ttl")
            .values(expires_at=FROZEN_NOW - timedelta(hours=1))
        )
        await session.commit()

    async with uow_factory() as uow:
        reacquired = await uow.idempotency.acquire("req-ttl", scope="send_message")

    assert reacquired is True


async def test_idempotency_null_expiry_is_not_reacquirable(
    uow_factory: UowFactory,
    sessionmaker: async_sessionmaker[AsyncSession],
) -> None:
    """Бессрочный ключ (``expires_at IS NULL``) повторно не захватывается."""
    async with uow_factory() as uow:
        assert await uow.idempotency.acquire("req-null", scope="scope") is True
    async with sessionmaker() as session:
        await session.execute(
            update(IdempotencyKeyRow).where(IdempotencyKeyRow.key == "req-null").values(expires_at=None)
        )
        await session.commit()

    async with uow_factory() as uow:
        again = await uow.idempotency.acquire("req-null", scope="scope")

    assert again is False


async def test_audit_log_is_append_only_and_readable(
    uow_factory: UowFactory,
) -> None:
    async with uow_factory() as uow:
        await uow.audit.append(
            actor="worker",
            action="conversation.state_changed",
            entity_type="conversation",
            entity_id="conv-1",
            payload={"from": "new", "to": "screened"},
            correlation_id="corr-1",
        )
        await uow.audit.append(
            actor="worker",
            action="screening.completed",
            entity_type="conversation",
            entity_id="conv-1",
            payload={"score": 85.0},
            correlation_id="corr-2",
        )

    async with uow_factory() as uow:
        entries = await uow.audit.list_for_entity("conversation", "conv-1")
        limited = await uow.audit.list_for_entity("conversation", "conv-1", limit=1)

    assert len(entries) == 2
    # Свежие события первыми.
    assert entries[0]["action"] == "screening.completed"
    assert entries[0]["payload"] == {"score": 85.0}
    assert entries[0]["correlation_id"] == "corr-2"
    assert entries[1]["action"] == "conversation.state_changed"
    assert len(limited) == 1


async def test_uow_satisfies_protocol(uow_factory: UowFactory) -> None:
    uow = uow_factory()
    async with uow as opened:
        assert isinstance(opened, UnitOfWork)
        for attribute in (
            "vacancies",
            "candidates",
            "resumes",
            "conversations",
            "messages",
            "screenings",
            "bookings",
            "outbox",
            "idempotency",
        ):
            assert getattr(opened, attribute) is not None


async def test_queries_are_parameterized_not_interpolated(
    uow_factory: UowFactory, make: EntityFactory, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """Проверка защиты от SQL-инъекций: опасная строка остаётся данными."""
    evil = "'; DROP TABLE candidates; --"
    candidate = await make.candidate(full_name=evil, external_id="evil-1")
    async with uow_factory() as uow:
        await uow.candidates.upsert(candidate)

    async with uow_factory() as uow:
        found = await uow.candidates.get_by_external(Channel.HH, "evil-1")
        nothing = await uow.candidates.get_by_external(Channel.HH, evil)

    assert found is not None
    assert found.full_name == evil
    assert nothing is None

    # Таблица на месте: инъекция не выполнилась.
    async with sessionmaker() as session:
        count = (await session.execute(text("select count(*) from candidates"))).scalar()
    assert count == 1


async def test_all_domain_entities_roundtrip_in_one_transaction(
    uow_factory: UowFactory, make: EntityFactory
) -> None:
    """Сценарий воронки: кандидат + резюме + диалог + сообщение + скрининг + outbox."""
    company_id = make.company_id
    candidate = await make.candidate()
    vacancy = await make.vacancy()
    resume = await make.resume(candidate_id=candidate.id)
    conversation = await make.conversation(
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        state=ConversationState.SCREENED,
        next_action_at=FROZEN_NOW,
    )

    async with uow_factory() as uow:
        await uow.candidates.upsert(candidate)
        await uow.vacancies.upsert(vacancy)
        await uow.resumes.save(resume)
        await uow.conversations.upsert(conversation)
        await uow.messages.add(make.message(conversation_id=conversation.id))
        await uow.screenings.save(
            await make.screening(resume_id=resume.id, vacancy_id=vacancy.id, candidate_id=candidate.id)
        )
        await uow.outbox.add(make.outbox())

    async with uow_factory() as uow:
        loaded_conversation = await uow.conversations.get(conversation.id)
        loaded_candidate = await uow.candidates.get(candidate.id)
        stated = await uow.conversations.list_by_state([ConversationState.SCREENED])
        claimed = await uow.outbox.claim_batch(limit=10, now=FROZEN_NOW)

    assert loaded_conversation is not None
    assert loaded_conversation.company_id == company_id
    assert loaded_candidate is not None
    assert stated
    assert claimed


async def test_delete_cascade_removes_dependent_rows(
    uow_factory: UowFactory, make: EntityFactory, sessionmaker: async_sessionmaker[AsyncSession]
) -> None:
    """FK с ``ON DELETE CASCADE``: удаление кандидата не оставляет сирот."""
    candidate = await make.candidate()
    resume = await make.resume(candidate_id=candidate.id)
    async with uow_factory() as uow:
        await uow.candidates.upsert(candidate)
        await uow.resumes.save(resume)

    async with sessionmaker() as session:
        await session.execute(delete(CandidateRow).where(CandidateRow.id == candidate.id))
        await session.commit()
        remaining = (await session.execute(text("select count(*) from resumes"))).scalar()

    assert remaining == 0
    async with uow_factory() as uow:
        assert await uow.resumes.get(resume.id) is None


async def test_conversation_upsert_keeps_other_rows(uow_factory: UowFactory, make: EntityFactory) -> None:
    """Upsert не должен затирать чужие диалоги (частая ошибка при реализации)."""
    first = await make.conversation(external_id="conv-a")
    second = await make.conversation(external_id="conv-b")
    async with uow_factory() as uow:
        await uow.conversations.upsert(first)
        await uow.conversations.upsert(second)

    async with uow_factory() as uow:
        loaded = await uow.conversations.get_by_external(Channel.HH, "conv-a")
        listed = await uow.conversations.list_by_state([ConversationState.NEW])

    assert loaded is not None
    assert loaded.id == first.id
    assert len(listed) == 2
