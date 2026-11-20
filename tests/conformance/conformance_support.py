"""Двойники и данные контрактного набора портов.

Зачем отдельный каталог: набор контрактных тестов описывает **порт**, а не
реализацию, и параметризован по всем зарегистрированным реализациям. Любой
будущий адаптер площадки (Avito, rabota.ru, Telegram) обязан пройти этот набор
без правок самих тестов — иначе после получения доступа к API мы снова будем
доказывать контракт вручную.

Имя модуля намеренно уникально (``conformance_support``, а не ``support``):
при ``importmode=prepend`` pytest добавляет каталог каждого теста в ``sys.path``,
и одинаковые имена модулей в разных каталогах конфликтуют в ``sys.modules``.

Что здесь есть:

* ``CHANNEL_IMPLEMENTATIONS`` / ``CALENDAR_IMPLEMENTATIONS`` — реестры фабрик
  реализаций портов. Сейчас в них тестовые двойники
  (``FakeChannelAdapter`` для hh.ru и Avito, ``FakeCalendarAdapter``);
  подключение боевого адаптера — это добавление одной фабрики, у которой
  транспорт подменён (``respx``), а сеть недоступна;
* ``ChannelHarness`` / ``CalendarHarness`` — реализация порта вместе с данными,
  которые нужны контрактным тестам, и с проверяемым «следом» действий.

Правило набора: **ассерты — только по методам порта**. Методы вида
``publish_response``/``add_busy`` — это подготовка данных двойника (у боевого
адаптера её место занимает мок транспорта), а не проверка поведения.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from hirebot.domain.entities import BusyInterval, InterviewSlot
from hirebot.domain.enums import Channel, MessageDirection
from hirebot.infra.clock import FakeClock
from hirebot.ports.channels import CalendarPort, ChannelPort
from hirebot.ports.dto import (
    InboundMessage,
    InboundResponse,
    ResumePayload,
    VacancyPayload,
)
from hirebot.testing.fakes import DEMO_RESUME_STRONG, FakeCalendarAdapter, FakeChannelAdapter

#: Опорное «сейчас» набора. Понедельник, 09:00 UTC = 12:00 по Москве: рабочие
#: часы, будний день — детерминированная сетка слотов без выходных.
NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

#: Окно подбора слотов в контрактных тестах календаря.
WINDOW_START = NOW
WINDOW_END = NOW + timedelta(days=5)

#: Длительность собеседования в контрактных тестах (как ``DEFAULT_INTERVIEW_DURATION_MINUTES``).
DURATION_MINUTES = 60

MANAGER_IDS: tuple[str, ...] = ("manager-1",)

VACANCY_EXTERNAL_ID = "conformance-vacancy-1"
RESUME_EXTERNAL_ID = "conformance-resume-1"
CLOSED_RESUME_EXTERNAL_ID = "conformance-resume-closed"


# --------------------------------------------------------------------------- #
# Канал
# --------------------------------------------------------------------------- #


def make_response(
    *,
    channel: Channel,
    conversation_external_id: str,
    candidate_external_id: str = "conformance-candidate-1",
    vacancy_external_id: str = VACANCY_EXTERNAL_ID,
    resume_external_id: str = RESUME_EXTERNAL_ID,
    received_at: datetime = NOW,
) -> InboundResponse:
    """Отклик кандидата для контрактных тестов канала."""
    return InboundResponse(
        channel=channel,
        conversation_external_id=conversation_external_id,
        candidate_external_id=candidate_external_id,
        vacancy_external_id=vacancy_external_id,
        candidate_name="Контрактный Кандидат",
        resume_external_id=resume_external_id,
        received_at=received_at,
        cover_letter="Здравствуйте! Заинтересовала вакансия.",
    )


def make_message(
    *,
    external_id: str,
    text: str,
    created_at: datetime,
    direction: MessageDirection = MessageDirection.INBOUND,
) -> InboundMessage:
    """Сообщение чата для контрактных тестов канала."""
    return InboundMessage(
        external_id=external_id,
        direction=direction,
        text=text,
        created_at=created_at,
    )


@dataclass(slots=True)
class ChannelHarness:
    """Реализация ``ChannelPort`` и данные, нужные контрактным тестам.

    ``port`` — то, что проверяется: все ассерты идут по методам порта
    (``send_message``/``list_messages`` и т.д.), а не по внутренностям адаптера.
    ``fake`` — подготовка данных у двойника: у боевого адаптера данные готовит
    мок транспорта, поэтому поле необязательное.
    ``action_trace`` — как наблюдать факт выполнения действия. У двойника это
    список выполненных ключей; у боевого адаптера — счётчик вызовов
    маршрута ``respx``. Фабрика реализации обязана это предоставить: «метод не
    бросил исключение» доказательством выполнения действия не является.
    """

    port: ChannelPort
    channel: Channel
    conversation_external_id: str
    candidate_external_id: str
    vacancy_external_id: str
    resume_external_id: str
    closed_resume_external_id: str
    action_trace: Callable[[], list[str]]
    fake: FakeChannelAdapter | None = None

    def publish_response(self, response: InboundResponse) -> None:
        """Опубликовать новый отклик на площадке (подготовка данных двойника)."""
        if self.fake is None:
            msg = "Подготовка данных доступна только у двойника канала"
            raise NotImplementedError(msg)
        self.fake.add_response(response)

    def publish_inbound(self, message: InboundMessage) -> None:
        """Положить входящее сообщение в чат (подготовка данных двойника)."""
        if self.fake is None:
            msg = "Подготовка данных доступна только у двойника канала"
            raise NotImplementedError(msg)
        self.fake.add_inbound_message(self.conversation_external_id, message)

    def performed_actions(self) -> list[str]:
        """Ключи действий, которые площадка действительно выполнила."""
        return self.action_trace()


def build_fake_channel_harness(channel: Channel) -> ChannelHarness:
    """Тестовый двойник канала с вакансией, откликом и двумя резюме.

    Одно резюме доступно, второе закрыто площадкой: контракт требует ровно
    ``ResumeAccessDenied`` на закрытое резюме, и для этого нужны оба случая.
    """
    adapter = FakeChannelAdapter(channel, clock=FakeClock(NOW))
    conversation_external_id = f"{channel.value}-conformance-chat-1"
    vacancy_external_id = f"{channel.value}-{VACANCY_EXTERNAL_ID}"
    resume_external_id = f"{channel.value}-{RESUME_EXTERNAL_ID}"
    closed_resume_external_id = f"{channel.value}-{CLOSED_RESUME_EXTERNAL_ID}"

    adapter.add_vacancy(
        VacancyPayload(
            external_id=vacancy_external_id,
            title="Python-разработчик",
            description="Разработка сервисов на Python, FastAPI, PostgreSQL.",
            city="Москва",
        )
    )
    adapter.add_response(
        make_response(
            channel=channel,
            conversation_external_id=conversation_external_id,
            vacancy_external_id=vacancy_external_id,
            resume_external_id=resume_external_id,
        )
    )
    adapter.add_resume(
        ResumePayload(
            external_id=resume_external_id,
            text=DEMO_RESUME_STRONG,
            candidate_name="Контрактный Кандидат",
            parsed={"timezone": "Europe/Moscow"},
        )
    )
    adapter.add_resume(
        ResumePayload(
            external_id=closed_resume_external_id,
            text="Резюме закрыто политикой площадки.",
            candidate_name="Закрытый Кандидат",
        )
    )
    adapter.deny_resume(closed_resume_external_id)

    return ChannelHarness(
        port=adapter,
        channel=channel,
        conversation_external_id=conversation_external_id,
        candidate_external_id="conformance-candidate-1",
        vacancy_external_id=vacancy_external_id,
        resume_external_id=resume_external_id,
        closed_resume_external_id=closed_resume_external_id,
        action_trace=lambda: [str(action_key) for _conversation, action_key in adapter.actions],
        fake=adapter,
    )


#: Реестр реализаций ``ChannelPort``. Чтобы подключить боевой адаптер, добавьте
#: фабрику, у которой HTTP-транспорт подменён моком (``respx``): тесты набора
#: обращаются только к методам порта и не знают, двойник это или сеть.
CHANNEL_IMPLEMENTATIONS: dict[str, Callable[[], ChannelHarness]] = {
    "hh_fake": lambda: build_fake_channel_harness(Channel.HH),
    "avito_fake": lambda: build_fake_channel_harness(Channel.AVITO),
}


async def outbound_texts(port: ChannelPort, conversation_external_id: str) -> list[str]:
    """Тексты исходящих сообщений диалога — только через методы порта.

    ``FakeChannelAdapter.texts_for`` для этого не годится: контрактный набор не
    должен знать о внутренностях реализации, а ``list_messages`` есть у всех.
    """
    messages = await port.list_messages(conversation_external_id)
    return [message.text for message in messages if message.direction is MessageDirection.OUTBOUND]


# --------------------------------------------------------------------------- #
# Календарь
# --------------------------------------------------------------------------- #


@dataclass(slots=True)
class CalendarHarness:
    """Реализация ``CalendarPort`` и параметры окна подбора слотов.

    ``now`` — опорное «сейчас» набора: контракт календаря требует не предлагать
    время в прошлом относительно именно этого момента.
    """

    port: CalendarPort
    now: datetime
    manager_ids: tuple[str, ...]
    start: datetime = WINDOW_START
    end: datetime = WINDOW_END
    fake: FakeCalendarAdapter | None = None
    busy: list[BusyInterval] = field(default_factory=list)

    def add_busy(self, start: datetime, end: datetime, *, manager_id: str | None = None) -> None:
        """Добавить занятость в календарь (подготовка данных двойника)."""
        if self.fake is None:
            msg = "Подготовка данных доступна только у двойника календаря"
            raise NotImplementedError(msg)
        self.fake.add_busy(BusyInterval(start=start, end=end, manager_id=manager_id))
        self.busy.append(BusyInterval(start=start, end=end, manager_id=manager_id))


def build_fake_calendar_harness() -> CalendarHarness:
    """Тестовый двойник календаря с теми же часами, что и у набора."""
    clock = FakeClock(NOW)
    adapter = FakeCalendarAdapter(clock=clock, manager_ids=list(MANAGER_IDS))
    return CalendarHarness(port=adapter, now=NOW, manager_ids=MANAGER_IDS, fake=adapter)


#: Реестр реализаций ``CalendarPort`` (Talantix сегодня, любой будущий — рядом).
CALENDAR_IMPLEMENTATIONS: dict[str, Callable[[], CalendarHarness]] = {
    "talantix_fake": build_fake_calendar_harness,
}


async def find_slots(
    harness: CalendarHarness,
    *,
    limit: int = 3,
    start: datetime | None = None,
    end: datetime | None = None,
    duration_minutes: int = DURATION_MINUTES,
) -> Sequence[InterviewSlot]:
    """``find_free_slots`` в окне набора (сокращение для читаемости тестов)."""
    return await harness.port.find_free_slots(
        manager_ids=list(harness.manager_ids),
        start=start if start is not None else harness.start,
        end=end if end is not None else harness.end,
        duration_minutes=duration_minutes,
        limit=limit,
    )
