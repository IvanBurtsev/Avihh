"""Двойники, данные и инъекция сбоев для тестов устойчивости.

Каждый тест этого каталога — сценарий реального инцидента: потерянный ответ
календаря после создания встречи, двойная бронь, ретраи outbox, повторная
доставка вебхука, дубль сообщения при повторе шага воронки, 429 с
``Retry-After`` и сетевой таймаут. Сбои вводятся **снаружи** портов (обёртками
над двойниками и моками транспорта), а не патчами внутренностей сервисов:
иначе тест доказывал бы работу патча, а не поведение системы.

Имя модуля намеренно уникально (``resilience_support``, а не ``support``):
pytest при ``importmode=prepend`` добавляет каталог теста в ``sys.path``, и
одинаковые имена модулей в разных каталогах конфликтуют в ``sys.modules``.

Сеть запрещена: HTTP-трафик идёт через боевой ``HttpxClient`` (ретраи, лимиты,
разбор ``Retry-After``), но перехватывается ``respx`` на уровне ``httpx``.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from pydantic import SecretStr

from hirebot.adapters.calendar.talantix.adapter import TalantixCalendarAdapter
from hirebot.adapters.channels.hh.adapter import HhChannelAdapter
from hirebot.adapters.locks import LocalLock
from hirebot.config import HHSettings, PolicySettings, QueueSettings, Settings, TalantixSettings
from hirebot.container import Container
from hirebot.domain.entities import (
    BusyInterval,
    Candidate,
    Conversation,
    OutboxEntry,
    Resume,
    Vacancy,
    new_id,
)
from hirebot.domain.enums import Channel, ConversationState, MessageDirection
from hirebot.domain.errors import ChannelUnavailable
from hirebot.infra.clock import FakeClock
from hirebot.infra.http import HttpxClient
from hirebot.ports.dto import InboundResponse, ResumePayload, VacancyPayload
from hirebot.ports.infra import NullCache
from hirebot.services.base import DEFAULT_COMPANY_ID
from hirebot.testing.fakes import (
    DEMO_CRITERIA,
    DEMO_RESUME_STRONG,
    DEMO_VACANCY_DESCRIPTION,
    FakeCalendarAdapter,
    FakeChannelAdapter,
    ImmediateRateLimiter,
    InMemoryJobQueue,
    InMemoryStore,
    InMemoryUnitOfWork,
    build_fake_container,
    fake_calendar,
    fake_channel,
    fake_queue,
    opened_store,
)

#: Опорное «сейчас»: понедельник, 09:00 UTC = 12:00 по Москве. Рабочие часы,
#: будний день — сетка слотов детерминирована, тихие часы не мешают.
FIXED_NOW = datetime(2026, 6, 1, 9, 0, tzinfo=UTC)

VACANCY_EXTERNAL_ID = "resilience-vacancy-1"
CONVERSATION_EXTERNAL_ID = "resilience-chat-1"
CANDIDATE_EXTERNAL_ID = "resilience-candidate-1"
RESUME_EXTERNAL_ID = "resilience-resume-1"
CANDIDATE_EMAIL = "candidate@example.com"

TIMEZONE = "Europe/Moscow"


# --------------------------------------------------------------------------- #
# Настройки, контейнер, хранилище
# --------------------------------------------------------------------------- #


def make_settings(
    *,
    policy: PolicySettings | None = None,
    queue: QueueSettings | None = None,
) -> Settings:
    """Настройки теста: окружение ``test``, без чтения ``.env`` и секретов."""
    return Settings(
        env="test",
        _env_file=None,
        policy=policy if policy is not None else PolicySettings(),
        queue=queue if queue is not None else QueueSettings(),
    )


def set_clock(container: Container, moment: datetime) -> None:
    """Выставить виртуальное время контейнера (все часы — через ``ClockPort``)."""
    setter = getattr(container.clock, "set", None)
    if not callable(setter):
        msg = "Часы контейнера не поддерживают установку времени: тест недетерминирован"
        raise RuntimeError(msg)
    setter(moment)


def advance_clock(container: Container, seconds: float) -> None:
    """Сдвинуть виртуальное время контейнера вперёд на ``seconds``.

    Нужно там, где пауза стала свойством хранилища (backoff outbox): без сдвига
    часов отложенная запись не должна забираться снова.
    """
    advance = getattr(container.clock, "advance", None)
    if not callable(advance):
        msg = "Часы контейнера не поддерживают сдвиг времени: тест недетерминирован"
        raise RuntimeError(msg)
    advance(seconds)


def make_container(*, settings: Settings | None = None) -> Container:
    """Пустой fake-контейнер с управляемым временем ``FIXED_NOW``."""
    container = build_fake_container(settings or make_settings(), seed_demo=False)
    set_clock(container, FIXED_NOW)
    return container


async def store_of(container: Container) -> InMemoryStore:
    """Хранилище fake-контейнера (только чтение: транзакция откатится)."""
    async with opened_store(container) as store:
        return store


async def outbox_entry(container: Container, entry_id: UUID) -> OutboxEntry:
    """Запись outbox по идентификатору (состояние попыток и ошибка)."""
    store = await store_of(container)
    return store.outbox[entry_id]


async def stored_messages(container: Container, conversation_id: UUID) -> list[Any]:
    """Сообщения диалога из БД (наблюдаемый результат шага воронки)."""
    store = await store_of(container)
    messages = [message for message in store.messages.values() if message.conversation_id == conversation_id]
    messages.sort(key=lambda message: message.created_at)
    return messages


async def seed_pipeline(
    container: Container,
    *,
    state: ConversationState = ConversationState.NEW,
    resume_text: str = DEMO_RESUME_STRONG,
    criteria: Sequence[dict[str, Any]] = DEMO_CRITERIA,
    suffix: str = "1",
) -> UUID:
    """Вакансия + кандидат + заготовка резюме + диалог в нужном состоянии.

    Резюме сохраняется **без текста** (как это делает ``IngestService``), а сам
    текст лежит в канале: так проверяется и штатный путь «догрузить резюме».

    ``suffix`` различает кандидатов одной вакансии: сценарии гонки двух
    кандидатов на одно время нуждаются в двух диалогах, а внешние
    идентификаторы ``(channel, external_id)`` уникальны.
    """
    now = container.clock.now()
    candidate_external_id = f"resilience-candidate-{suffix}"
    conversation_external_id = f"resilience-chat-{suffix}"
    resume_external_id = f"resilience-resume-{suffix}"
    vacancy = Vacancy(
        id=new_id(),
        channel=Channel.HH,
        external_id=VACANCY_EXTERNAL_ID,
        title="Python-разработчик",
        company_id=DEFAULT_COMPANY_ID,
        description=DEMO_VACANCY_DESCRIPTION,
        criteria=[dict(criterion) for criterion in criteria],
        created_at=now,
        updated_at=now,
    )
    candidate = Candidate(
        id=new_id(),
        channel=Channel.HH,
        external_id=candidate_external_id,
        full_name="Тестовый Кандидат",
        company_id=DEFAULT_COMPANY_ID,
        email=CANDIDATE_EMAIL,
        created_at=now,
    )
    conversation = Conversation(
        id=new_id(),
        company_id=DEFAULT_COMPANY_ID,
        candidate_id=candidate.id,
        vacancy_id=vacancy.id,
        channel=Channel.HH,
        external_id=conversation_external_id,
        state=state,
        created_at=now,
        updated_at=now,
    )
    resume = Resume(
        id=new_id(),
        candidate_id=candidate.id,
        channel=Channel.HH,
        external_id=resume_external_id,
        fetched_at=now,
    )

    async with container.uow_factory() as uow:
        await uow.vacancies.upsert(vacancy)
        await uow.candidates.upsert(candidate)
        await uow.conversations.upsert(conversation)
        await uow.resumes.save(resume)
        await uow.commit()

    channel = fake_channel(container)
    channel.add_vacancy(
        VacancyPayload(
            external_id=VACANCY_EXTERNAL_ID,
            title=vacancy.title,
            description=vacancy.description,
            city="Москва",
        )
    )
    channel.add_response(
        InboundResponse(
            channel=Channel.HH,
            conversation_external_id=conversation_external_id,
            candidate_external_id=candidate_external_id,
            vacancy_external_id=VACANCY_EXTERNAL_ID,
            candidate_name=candidate.full_name,
            resume_external_id=resume_external_id,
            received_at=now,
        )
    )
    channel.add_resume(
        ResumePayload(
            external_id=resume_external_id,
            text=resume_text,
            candidate_name=candidate.full_name,
            parsed={"timezone": TIMEZONE},
        )
    )
    return conversation.id


async def publish_incoming(
    container: Container,
    *,
    conversation_external_id: str = CONVERSATION_EXTERNAL_ID,
    candidate_external_id: str = CANDIDATE_EXTERNAL_ID,
    resume_external_id: str = RESUME_EXTERNAL_ID,
    resume_text: str = DEMO_RESUME_STRONG,
) -> InboundResponse:
    """Опубликовать отклик на площадке, ничего не создавая в БД.

    Это состояние «вебхук пришёл / поллер увидел отклик»: диалог ещё не создан,
    его создаёт ``IngestService``.
    """
    now = container.clock.now()
    channel = fake_channel(container)
    channel.add_vacancy(
        VacancyPayload(
            external_id=VACANCY_EXTERNAL_ID,
            title="Python-разработчик",
            description=DEMO_VACANCY_DESCRIPTION,
            city="Москва",
        )
    )
    channel.add_resume(
        ResumePayload(
            external_id=resume_external_id,
            text=resume_text,
            candidate_name="Тестовый Кандидат",
            parsed={"timezone": TIMEZONE},
        )
    )
    response = InboundResponse(
        channel=Channel.HH,
        conversation_external_id=conversation_external_id,
        candidate_external_id=candidate_external_id,
        vacancy_external_id=VACANCY_EXTERNAL_ID,
        candidate_name="Тестовый Кандидат",
        resume_external_id=resume_external_id,
        received_at=now,
    )
    channel.add_response(response)
    return response


async def rewind_last_step(
    container: Container,
    conversation_id: UUID,
    *,
    state: ConversationState,
) -> None:
    """Откатить последнюю транзакцию шага: сообщение ушло в чат, в БД следов нет.

    Это ровно тот инцидент, ради которого существует ``idempotency_key``
    (ARCHITECTURE.md, п. 2.4): воркер упал между отправкой и фиксацией, шаг
    повторится, и кандидат не должен получить второе такое же сообщение.

    ``state`` — точка возобновления, в которой диалог остался после падения.
    Для первого контакта это ``SCREENED``: веха ``SCREENED`` коммитится
    отдельной транзакцией **до** отправки (``ConversationService._mark_screened``)
    и падением последнего шага не откатывается. Поэтому откатывать состояние в
    ``NEW`` нельзя — такого состояния после падения уже не существует.
    """
    async with container.uow_factory() as uow:
        store = cast(InMemoryUnitOfWork, uow).store
        conversation = store.conversations[conversation_id]
        conversation.state = state
        conversation.auto_messages_sent = 0
        conversation.last_inbound_at = None
        conversation.last_outbound_at = None
        removed = [
            message_id
            for message_id, message in store.messages.items()
            if message.conversation_id == conversation_id and message.direction is MessageDirection.OUTBOUND
        ]
        for message_id in removed:
            message = store.messages.pop(message_id)
            if message.external_id:
                store.message_external_ids.pop((conversation_id, message.external_id), None)
        await uow.commit()


async def add_outbox_entry(
    container: Container,
    *,
    topic: str,
    payload: dict[str, Any] | None = None,
    dedupe_key: str = "",
) -> UUID:
    """Положить задачу в outbox так же, как это делает сервис (``available_at = now``)."""
    now = container.clock.now()
    entry = OutboxEntry(
        id=new_id(),
        topic=topic,
        payload=dict(payload or {}),
        dedupe_key=dedupe_key or f"{topic}:{new_id()}",
        available_at=now,
        created_at=now,
    )
    async with container.uow_factory() as uow:
        saved = await uow.outbox.add(entry)
        await uow.commit()
    return saved.id


# --------------------------------------------------------------------------- #
# Инъекция сбоев календаря
# --------------------------------------------------------------------------- #


class TimeoutAfterCreate(FakeCalendarAdapter):
    """Календарь, у которого теряется ответ на создание встречи.

    Встреча при этом **реально создаётся** — это случай «Таймаут после
    создания» из ARCHITECTURE.md, п. 6.3. Мы не знаем, создана встреча или
    нет, и обязаны выяснить это идемпотентным маркером и перечитыванием.
    """

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.lost_responses = 0

    async def create_meeting(self, **options: Any) -> object:
        meeting = await super().create_meeting(**options)
        if self.lost_responses < 1:
            self.lost_responses += 1
            msg = "Таймаут: ответ на создание встречи потерян (fake)"
            raise ChannelUnavailable(msg)
        return meeting


class BusyAppearingOnCreate(FakeCalendarAdapter):
    """Календарь, в котором занятость появляется ровно перед созданием встречи.

    Гонка из ARCHITECTURE.md, п. 6.6: двое кандидатов выбирают одно время, и
    чужое бронирование становится видно между подбором слота и созданием
    встречи. Так проверяется обещание «повторная проверка занятости перед
    созданием».
    """

    def __init__(self, **options: Any) -> None:
        super().__init__(**options)
        self.races: list[BusyInterval] = []

    async def create_meeting(self, **options: Any) -> object:
        managers = list(options.get("manager_ids") or self.manager_ids)
        interval = BusyInterval(
            start=options["start"],
            end=options["end"],
            manager_id=managers[0] if managers else None,
            title="Чужая бронь",
        )
        self.races.append(interval)
        self.busy.append(interval)
        return await super().create_meeting(**options)


class BlindBusyCalendar(FakeCalendarAdapter):
    """Календарь, который **не** сообщает занятость (худший случай).

    Площадка не валидирует конфликты и может не отдавать занятость вовсе: тогда
    ни проверка перед созданием встречи, ни проверка после неё ничего не видят.
    Единственный рубеж, который обязан сработать, — ограничение БД на активные
    брони (``bookings(manager_id, starts_at)``), и этот двойник позволяет его
    проверить в быстром контуре.
    """

    async def list_busy(
        self, *, manager_ids: Sequence[str], start: datetime, end: datetime
    ) -> Sequence[BusyInterval]:
        return []


def install_calendar(container: Container, calendar: FakeCalendarAdapter) -> FakeCalendarAdapter:
    """Подменить календарь контейнера (сервисы читают его из ``Container``)."""
    container.calendar = calendar
    return fake_calendar(container)


# --------------------------------------------------------------------------- #
# HTTP: боевой клиент с перехваченным транспортом
# --------------------------------------------------------------------------- #


def make_http_client(
    *,
    clock: FakeClock | None = None,
    max_attempts: int = 3,
    max_retry_after_s: float = 2.0,
) -> HttpxClient:
    """Боевой ``HttpxClient``: ретраи, ``Retry-After`` и разбор ошибок — как в проде.

    Часы виртуальные: паузы между повторами видны в ``clock.slept`` и не
    замедляют тест.
    """
    return HttpxClient(
        cache=NullCache(),
        rate_limiter=ImmediateRateLimiter(),
        locks=LocalLock(),
        clock=clock if clock is not None else FakeClock(FIXED_NOW),
        max_attempts=max_attempts,
        max_retry_after_s=max_retry_after_s,
    )


def hh_settings() -> HHSettings:
    """Настройки hh.ru для тестов (токен задан, срок жизни неизвестен)."""
    return HHSettings(
        enabled=True,
        base_url="https://api.hh.ru",
        client_id="test-client",
        client_secret=SecretStr("test-secret"),
        access_token=SecretStr("test-access-token"),
        user_agent="hirebot-test/0.1 (qa@example.com)",
    )


def talantix_settings() -> TalantixSettings:
    """Настройки Talantix для тестов (зона по умолчанию — Москва)."""
    return TalantixSettings(
        enabled=True,
        base_url="https://api.talantix.ru",
        access_token=SecretStr("talantix-token"),
        default_timezone=TIMEZONE,
        max_window_days=90,
    )


def make_hh_adapter(
    http: HttpxClient,
    *,
    clock: FakeClock | None = None,
) -> HhChannelAdapter:
    """Адаптер hh.ru поверх боевого HTTP-клиента (транспорт перехватывает respx)."""
    return HhChannelAdapter(
        hh_settings(),
        http=http,
        clock=clock if clock is not None else FakeClock(FIXED_NOW),
        cache=NullCache(),
    )


def make_talantix_adapter(
    http: HttpxClient,
    *,
    clock: FakeClock | None = None,
) -> TalantixCalendarAdapter:
    """Адаптер календаря Talantix поверх боевого HTTP-клиента."""
    return TalantixCalendarAdapter(
        talantix_settings(),
        http=http,
        clock=clock if clock is not None else FakeClock(FIXED_NOW),
        cache=NullCache(),
    )


def negotiation_payload(*, conversation_id: str, messaging_status: str = "ok") -> dict[str, Any]:
    """Ответ ``GET /negotiations/{id}`` hh.ru: доступность переписки и действия."""
    return {
        "id": conversation_id,
        "messaging_status": messaging_status,
        "counters": {"messages": 1, "unread_messages": 0},
        "actions": [
            {
                "id": "invitation",
                "name": "Пригласить",
                "enabled": True,
                "method": "PUT",
                "url": f"https://api.hh.ru/negotiations/{conversation_id}",
                "arguments": [{"id": "message", "required": True}],
            }
        ],
    }


# --------------------------------------------------------------------------- #
# Прочее
# --------------------------------------------------------------------------- #


def http_url(base: str, path: str) -> str:
    """Собрать URL для мока ``respx`` (без ручной склейки в тестах)."""
    return f"{base.rstrip('/')}/{path.lstrip('/')}"


def channel_of(container: Container) -> FakeChannelAdapter:
    return fake_channel(container)


def calendar_of(container: Container) -> FakeCalendarAdapter:
    return fake_calendar(container)


def queue_of(container: Container) -> InMemoryJobQueue:
    return fake_queue(container)
