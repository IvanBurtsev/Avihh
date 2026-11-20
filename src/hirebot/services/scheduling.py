"""Расписание собеседований: подбор слотов и бронь с верификацией.

Два принципа, заложенных в архитектуру (ARCHITECTURE.md, п. 3.3):

* **слоты не выдумываются.** Свободных слотов площадка не отдаёт: сервис
  строит сетку сам и сообщает календарю только занятость. Если календарь не
  сконфигурирован, честный ответ — пустой список, а диалог уводит человек;
* **бронь подтверждается перечитыванием.** Ответ на создание встречи может
  потеряться в таймауте, и тогда мы не знаем, создана встреча или нет.
  Единственный надёжный способ — перечитать её по ``external_id``. Пока
  встреча не перечитана, ``Booking`` не сохраняется и состояние диалога не
  меняется.

Слот адресуется **временем** (``slot_start``), а не индексом в пересобранной
сетке: индекс не является инвариантом шага — между попытками сетка сдвигается
(занятость появилась или пропала), и тот же индекс указывал бы на другое время.
``slot_index`` сохранён для обратной совместимости и означает «индекс в текущей
сетке»; новые задачи обязаны передавать ``slot_start``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import cast
from uuid import UUID

from hirebot.container import Container
from hirebot.domain.entities import Booking, BusyInterval, InterviewSlot, new_id
from hirebot.domain.enums import BookingStatus, ConversationState
from hirebot.domain.errors import (
    ChannelNotConfigured,
    ChannelUnavailable,
    DuplicateAction,
    NotFound,
    SlotConflict,
)
from hirebot.logging_setup import get_logger
from hirebot.observability.metrics import record_calendar_event, record_slot_conflict
from hirebot.ports.channels import CalendarPort
from hirebot.services.base import ServiceBase
from hirebot.services.transitions import apply_transition, transition_path

logger = get_logger(__name__)

#: Длительность собеседования по умолчанию. В конфиге отдельного поля нет:
#: когда появится, значение переедет в ``TalantixSettings.duration_minutes``.
DEFAULT_INTERVIEW_DURATION_MINUTES = 60

#: Сколько вариантов времени предлагаем кандидату за раз.
DEFAULT_SLOT_LIMIT = 3

#: Сколько слотов просматриваем, разыскивая слот по точному времени.
#: Сетка строится календарём, поэтому «найти слот №7» — это попросить сетку
#: достаточной длины; 32 — с запасом на несколько рабочих дней.
SLOT_LOOKUP_LIMIT = 32


def meeting_external_id(meeting: object) -> str | None:
    """Достать ``external_id`` из ответа календаря.

    Порт возвращает ``object``: адаптер Talantix отдаёт dataclass, но тестовые
    и будущие адаптеры могут вернуть словарь. Поддерживаем оба варианта и не
    полагаемся на конкретный тип.
    """
    value = getattr(meeting, "external_id", None)
    if value is None and isinstance(meeting, Mapping):
        value = meeting.get("external_id")
    return str(value) if value else None


def _ensure_aware(moment: datetime) -> datetime:
    """Проверить, что момент времени timezone-aware.

    Наивная дата — ошибка вызывающего кода: без зоны невозможно понять, о каком
    моменте речь, а «молча предположить Москву» здесь дороже всего.
    """
    if moment.tzinfo is None or moment.tzinfo.utcoffset(moment) is None:
        msg = f"Время слота должно быть timezone-aware: {moment!r}"
        raise NotFound(msg)
    return moment


def _overlaps(interval: BusyInterval, start: datetime, end: datetime) -> bool:
    """Пересекается ли занятость с интервалом ``[start, end)``."""
    return interval.start < end and start < interval.end


def _is_same_interval(interval: BusyInterval, *, start: datetime, end: datetime, manager_id: str) -> bool:
    """Совпадает ли интервал ровно с нашим слотом (та же длительность и менеджер).

    Так выглядит **собственная** встреча: порт отдаёт занятость без
    идентификаторов, поэтому «наша» встреча распознаётся по точному интервалу.
    Чужая бронь может совпасть с нами временем — именно поэтому одной проверки
    «это наш интервал?» мало и дополнительно считается их количество.
    """
    if interval.start != start or interval.end != end:
        return False
    return interval.manager_id is None or interval.manager_id == manager_id


@dataclass(slots=True)
class _BookingContext:
    """Данные диалога, нужные брони."""

    candidate_id: UUID
    vacancy_id: UUID
    vacancy_title: str
    candidate_email: str | None
    state: ConversationState


class SchedulingService(ServiceBase):
    """Слоты и бронь собеседования через ``CalendarPort``."""

    def __init__(self, container: Container) -> None:
        super().__init__(container)

    async def propose_slots(
        self,
        *,
        conversation_id: UUID,
        days_ahead: int = 5,
        limit: int = DEFAULT_SLOT_LIMIT,
    ) -> list[InterviewSlot]:
        """Подобрать свободные слоты в окне ``days_ahead`` дней.

        Окно ограничено ``settings.talantix.max_window_days`` (у Talantix запрос
        шире 93 дней — ошибка). Календарь не сконфигурирован → пустой список.
        """
        calendar = self._container.calendar
        if calendar is None:
            logger.warning(
                "calendar_not_configured",
                conversation_id=str(conversation_id),
                hint="задайте HIREBOT_TALANTIX__ENABLED=true и токен",
            )
            return []

        now = self.now()
        window_days = max(1, min(days_ahead, self.settings.talantix.max_window_days))
        # Окно начинаем с начала часа: сетка слотов тогда не «плывёт» между
        # предложением и ответом кандидата. Порт не даёт места, где хранить
        # предложенные слоты (см. отчёт), поэтому список пересчитывается
        # детерминированно, и стабильная точка отсчёта здесь принципиальна.
        window_start = now.replace(minute=0, second=0, microsecond=0)
        end = now + timedelta(days=window_days)
        slots = await calendar.find_free_slots(
            manager_ids=self._manager_ids(),
            start=window_start,
            end=end,
            duration_minutes=DEFAULT_INTERVIEW_DURATION_MINUTES,
            limit=max(1, limit),
        )
        # Прошедшее время предлагать нельзя: округление окна вниз по часу
        # могло подмешать уже начавшиеся интервалы.
        future = [slot for slot in slots if slot.start > now]
        logger.info(
            "slots_proposed",
            conversation_id=str(conversation_id),
            count=len(future),
            window_days=window_days,
        )
        return future

    async def book(
        self,
        *,
        conversation_id: UUID,
        slot_index: int | None = None,
        slot_start: datetime | None = None,
        requested_by: str = "bot",
    ) -> Booking:
        """Забронировать слот и подтвердить бронь перечитыванием встречи.

        Слот адресуется либо точным временем ``slot_start`` (предпочтительно),
        либо индексом ``slot_index`` в текущей сетке (обратная совместимость).

        Идемпотентность держится на ключе ``booking:{conversation_id}:{start}``:
        повторный вызов с тем же слотом возвращает существующую бронь и не
        создаёт вторую встречу.

        Порядок шагов (ARCHITECTURE.md, п. 3.3 и 6.6):

        1. транзакционный захват (ключ идемпотентности + проверка занятости по БД);
        2. **повторная** проверка занятости в календаре — уже внутри транзакции;
        3. создание встречи;
        4. проверка занятости ещё раз (площадка не валидирует конфликты: если
           время заняли между шагами 2 и 3, встреча отменяется, а бронь падает
           ``SlotConflict``);
        5. верификация перечитыванием и коммит.
        """
        if slot_index is not None and slot_index < 0:
            msg = f"Индекс слота не может быть отрицательным: {slot_index}"
            raise NotFound(msg)
        if slot_index is None and slot_start is None:
            msg = "Не указан слот: нужен slot_start (ISO, aware) или slot_index"
            raise NotFound(msg)

        context = await self._load(conversation_id)
        slot = await self._resolve_slot(
            conversation_id=conversation_id,
            slot_index=slot_index,
            slot_start=slot_start,
        )
        idempotency_key = f"booking:{conversation_id}:{slot.start.isoformat()}"
        calendar = self._require_calendar()

        existing = await self._find_existing(idempotency_key)
        if existing is not None:
            # Повтор шага: бронь уже есть. Но если за это время время заняли,
            # подтверждать её нельзя — это разбор для человека, а не «ок».
            if await self._is_occupied_by_others(calendar, slot):
                raise SlotConflict(
                    f"Слот {slot.start.isoformat()} занят: существующую бронь "
                    f"{existing.id} подтвердить нельзя, нужен разбор человеком"
                )
            logger.info(
                "booking_idempotent_hit",
                conversation_id=str(conversation_id),
                booking_id=str(existing.id),
                idempotency_key=idempotency_key,
            )
            await self._mark_booked(conversation_id)
            return existing

        # Проверяем путь в FSM заранее: не хочется создавать встречу в календаре
        # и только потом узнать, что из текущего состояния бронь невозможна.
        transition_path(context.state, ConversationState.BOOKED)

        booking = Booking(
            id=new_id(),
            conversation_id=conversation_id,
            candidate_id=context.candidate_id,
            vacancy_id=context.vacancy_id,
            manager_id=slot.manager_id,
            start=slot.start,
            end=slot.end,
            timezone=slot.timezone,
            status=BookingStatus.CONFIRMED,
            idempotency_key=idempotency_key,
        )
        try:
            saved = await self._persist(
                booking=booking,
                conversation_id=conversation_id,
                calendar=calendar,
                slot=slot,
                vacancy_title=context.vacancy_title,
                candidate_email=context.candidate_email,
            )
        except SlotConflict:
            # Конфликт слота — бизнес-событие, а не сбой: считаем отдельно от
            # прочих ошибок, чтобы дежурный видел, что календарь «горячий».
            record_slot_conflict()
            raise
        logger.info(
            "interview_booked",
            conversation_id=str(conversation_id),
            booking_id=str(saved.id),
            start=saved.start.isoformat(),
            manager_id=saved.manager_id,
            requested_by=requested_by,
        )
        return saved

    # ------------------------------------------------------------------ #
    # Внутреннее
    # ------------------------------------------------------------------ #

    async def _resolve_slot(
        self,
        *,
        conversation_id: UUID,
        slot_index: int | None,
        slot_start: datetime | None,
    ) -> InterviewSlot:
        """Найти слот по времени (предпочтительно) или по индексу в сетке.

        Для ``slot_start`` время обязано совпасть с найденным слотом: если его
        в сетке нет — это ``SlotConflict``, а не «подберём другое время».
        Исключение одно: собственная встреча, созданная предыдущей попыткой
        (ответ потерялся в таймауте). Такую встречу перечитывает шаг
        верификации по маркеру идемпотентности, поэтому её время считается
        допустимым.
        """
        if slot_start is not None:
            target = _ensure_aware(slot_start)
            slots = await self.propose_slots(conversation_id=conversation_id, limit=SLOT_LOOKUP_LIMIT)
            for slot in slots:
                if slot.start == target:
                    return slot
            own = await self._slot_of_previous_attempt(target)
            if own is not None:
                return own
            msg = (
                f"Слот {target.isoformat()} не найден среди свободных: время занято "
                "или больше не предлагается. Подбирать другое время молча нельзя"
            )
            raise SlotConflict(msg)

        assert slot_index is not None  # проверено в ``book``
        slots = await self.propose_slots(conversation_id=conversation_id, limit=slot_index + 1)
        if slot_index >= len(slots):
            msg = (
                f"Слот с индексом {slot_index} не найден среди предложенных "
                f"(доступно {len(slots)}). Индекс считается с нуля по текущей сетке календаря; "
                "надёжнее адресовать слот временем (slot_start)"
            )
            raise NotFound(msg)
        return slots[slot_index]

    async def _slot_of_previous_attempt(self, target: datetime) -> InterviewSlot | None:
        """Слот, который занят ровно нашим интервалом (встреча уже создана).

        Это случай «таймаут после создания» (ARCHITECTURE.md, п. 6.3): встреча в
        календаре есть, но ответ до нас не дошёл, поэтому в сетке её время уже
        занято. Если занятость совпадает с запрошенным временем ровно, считаем
        это собственной встречей и доводим шаг до верификации по маркеру.
        """
        calendar = self._require_calendar()
        end = target + timedelta(minutes=DEFAULT_INTERVIEW_DURATION_MINUTES)
        manager_ids = self._manager_ids()
        busy = cast(
            "Sequence[BusyInterval]",
            await calendar.list_busy(manager_ids=manager_ids, start=target, end=end),
        )
        for interval in busy:
            if interval.start == target and interval.end == end:
                manager_id = interval.manager_id or (manager_ids[0] if manager_ids else "")
                if not manager_id:
                    return None
                return InterviewSlot(
                    start=target,
                    end=end,
                    manager_id=manager_id,
                    timezone=self.settings.talantix.default_timezone,
                )
        return None

    async def _load(self, conversation_id: UUID) -> _BookingContext:
        async with self.uow() as uow:
            conversation = await uow.conversations.get_with_lock(conversation_id)
            if conversation is None:
                msg = f"Диалог {conversation_id} не найден"
                raise NotFound(msg)
            vacancy = await uow.vacancies.get(conversation.vacancy_id)
            candidate = await uow.candidates.get(conversation.candidate_id)
        return _BookingContext(
            candidate_id=conversation.candidate_id,
            vacancy_id=conversation.vacancy_id,
            vacancy_title=vacancy.title if vacancy is not None else "вакансия",
            candidate_email=candidate.email if candidate is not None else None,
            state=conversation.state,
        )

    async def _find_existing(self, idempotency_key: str) -> Booking | None:
        async with self.uow() as uow:
            return await uow.bookings.get_by_idempotency_key(idempotency_key)

    async def _is_occupied_by_others(self, calendar: CalendarPort, slot: InterviewSlot) -> bool:
        """Занят ли слот **чужой** встречей.

        Проверка нужна после подбора слота и после создания встречи: площадка не
        валидирует конфликты, поэтому единственный источник правды — сама
        занятость. Своя встреча (созданная этой же попыткой или предыдущей)
        конфликтом не считается — иначе повтор шага не смог бы довести бронь до
        конца. Чужая встреча, совпавшая с нами временем, видна как второй
        интервал в том же слоте.
        """
        busy = cast(
            "Sequence[BusyInterval]",
            await calendar.list_busy(
                manager_ids=[slot.manager_id],
                start=slot.start,
                end=slot.end,
            ),
        )
        overlapping = [interval for interval in busy if _overlaps(interval, slot.start, slot.end)]
        if not overlapping:
            return False
        ours = [
            interval
            for interval in overlapping
            if _is_same_interval(interval, start=slot.start, end=slot.end, manager_id=slot.manager_id)
        ]
        return len(ours) != len(overlapping) or len(ours) > 1

    async def _persist(
        self,
        *,
        booking: Booking,
        conversation_id: UUID,
        calendar: CalendarPort,
        slot: InterviewSlot,
        vacancy_title: str,
        candidate_email: str | None,
    ) -> Booking:
        """Захватить слот, создать встречу, верифицировать и сохранить бронь.

        Всё — в одной транзакции: захват слота (ключ идемпотентности + проверка
        занятости по БД) выполняется до создания встречи, а коммит — только
        после верификации. Гонку двух воркеров дополнительно ловит частичный
        уникальный индекс ``bookings(manager_id, starts_at)``: нарушение
        ограничения транслируется в ``SlotConflict``.
        """
        async with self.uow() as uow:
            acquired = await uow.idempotency.acquire(booking.idempotency_key, scope="booking")
            if not acquired:
                # Ключ занят: параллельный воркер уже довёл эту бронь до конца.
                existing = await uow.bookings.get_by_idempotency_key(booking.idempotency_key)
                if existing is not None:
                    await uow.commit()
                    await self._mark_booked(conversation_id)
                    return existing
                await uow.rollback()
                msg = (
                    f"Ключ идемпотентности {booking.idempotency_key} уже занят, "
                    "но бронь не найдена: состояние требует разбора человеком"
                )
                raise DuplicateAction(msg)

            overlap = await uow.bookings.find_active_overlap(
                booking.manager_id, start=booking.start, end=booking.end
            )
            if overlap is not None:
                await uow.rollback()
                msg = (
                    f"Слот {booking.start.isoformat()} у менеджера {booking.manager_id} уже занят "
                    f"бронью {overlap.id}: двойная бронь недопустима"
                )
                raise SlotConflict(msg)

            if await self._is_occupied_by_others(calendar, slot):
                await uow.rollback()
                msg = f"Слот {slot.start.isoformat()} занят по данным календаря: создавать встречу нельзя"
                raise SlotConflict(msg)

            meeting = await calendar.create_meeting(
                title=f"Собеседование: {vacancy_title}",
                start=slot.start,
                end=slot.end,
                timezone=slot.timezone,
                manager_ids=[slot.manager_id],
                candidate_email=candidate_email,
                description=f"[hirebot:{booking.idempotency_key}] Собеседование: {vacancy_title}",
                idempotency_key=booking.idempotency_key,
            )
            external_meeting_id = meeting_external_id(meeting)
            record_calendar_event("created")
            if not external_meeting_id:
                msg = "Календарь не вернул идентификатор встречи: бронь не подтверждена"
                raise ChannelUnavailable(msg)

            # Площадка не проверяет конфликты сама: если время заняли между
            # проверкой и созданием, лишнюю встречу надо немедленно отменить.
            if await self._is_occupied_by_others(calendar, slot):
                await uow.rollback()
                await self._cancel_best_effort(calendar, external_meeting_id)
                msg = (
                    f"Слот {slot.start.isoformat()} заняли во время создания встречи: "
                    "лишняя встреча отменена, бронь не подтверждена"
                )
                raise SlotConflict(msg)

            verified = await calendar.get_meeting(external_meeting_id)
            if verified is None:
                msg = (
                    f"Встреча {external_meeting_id} не найдена при перечитывании: "
                    "бронь не подтверждена, сообщение кандидату менять нельзя"
                )
                raise ChannelUnavailable(msg)

            booking.external_meeting_id = external_meeting_id
            try:
                saved = await uow.bookings.save(booking)
            except SlotConflict as exc:
                # Сработало ограничение БД (частичный уникальный индекс
                # ``bookings(manager_id, starts_at)``): гонку двух кандидатов на
                # одно время выиграл кто-то другой. Свою встречу отменяем.
                await uow.rollback()
                await self._cancel_best_effort(calendar, external_meeting_id)
                msg = (
                    f"Слот {booking.start.isoformat()} у менеджера {booking.manager_id} "
                    "уже занят: бронь не создана"
                )
                raise SlotConflict(msg) from exc

            conversation = await uow.conversations.get_with_lock(conversation_id)
            if conversation is not None:
                conversation.state = apply_transition(conversation.state, ConversationState.BOOKED)
                conversation.updated_at = self.now()
                await uow.conversations.upsert(conversation)
            await uow.commit()
        return saved

    async def _mark_booked(self, conversation_id: UUID) -> None:
        """Идемпотентно привести диалог в ``BOOKED`` (для повторной брони)."""
        async with self.uow() as uow:
            conversation = await uow.conversations.get_with_lock(conversation_id)
            if conversation is None or conversation.state is ConversationState.BOOKED:
                return
            if conversation.is_terminal:
                logger.warning(
                    "booking_for_terminal_conversation",
                    conversation_id=str(conversation_id),
                    state=conversation.state.value,
                )
                return
            conversation.state = apply_transition(conversation.state, ConversationState.BOOKED)
            conversation.updated_at = self.now()
            await uow.conversations.upsert(conversation)
            await uow.commit()

    async def _cancel_best_effort(self, calendar: CalendarPort, external_meeting_id: str) -> None:
        """Отменить лишнюю встречу; ошибка отмены не должна валить обработку."""
        try:
            await calendar.cancel_meeting(external_meeting_id)
        except Exception as exc:
            logger.warning(
                "cancel_meeting_failed",
                external_meeting_id=external_meeting_id,
                error=str(exc),
            )

    def _require_calendar(self) -> CalendarPort:
        try:
            return self._container.require_calendar()
        except RuntimeError as exc:
            raise ChannelNotConfigured(str(exc)) from exc

    def _manager_ids(self) -> list[str]:
        """Кто участвует в собеседовании.

        Реестра менеджеров в настройках нет, поэтому собираем то, что есть:
        ``hh.manager_id`` плюс ``talantix.manager_id``, если он появится в
        конфиге. Пустой список адаптер календаря трактует как «любой доступный
        менеджер».
        """
        manager_ids: list[str] = []
        hh_manager = self.settings.hh.manager_id
        if hh_manager:
            manager_ids.append(hh_manager)
        talantix_manager = getattr(self.settings.talantix, "manager_id", None)
        if isinstance(talantix_manager, str) and talantix_manager:
            manager_ids.append(talantix_manager)
        return manager_ids
