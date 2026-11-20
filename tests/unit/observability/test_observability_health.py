"""Тесты расширенной готовности (``hirebot.observability.health``).

Проверяется главное эксплуатационное свойство: отказ **необязательной**
зависимости (календарь, LLM, один из каналов) не выводит инстанс из
балансировки, а отказ БД и Redis в режиме ``arq`` — выводит.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from observability_support import (
    CalendarWithoutHealthcheck,
    FakeCalendar,
    FakeChannel,
    FakeLlmWithHealthcheck,
    FakeUowFactory,
    make_container,
    make_settings,
)

from hirebot.domain.enums import Channel
from hirebot.observability.health import (
    CHANNEL_PREFIX,
    DependencyStatus,
    ReadinessReport,
    check_calendar,
    check_channels,
    check_database,
    check_dependencies,
    check_llm,
    check_redis,
    redis_required,
)

# --------------------------------------------------------------------------- #
# БД
# --------------------------------------------------------------------------- #


async def test_database_is_checked_with_select_one() -> None:
    """Успешная проверка БД выполняет ``SELECT 1`` в одной транзакции."""
    factory = FakeUowFactory()
    status = await check_database(make_container(uow_factory=factory))

    assert status.ok is True
    assert status.critical is True
    assert factory.created[0].session is not None
    assert factory.created[0].session.statements == ["SELECT 1"]


async def test_database_falls_back_to_repository_without_session() -> None:
    """Fake-UoW без ``session`` проверяется чтением через репозиторий."""
    factory = FakeUowFactory(with_session=False)
    status = await check_database(make_container(uow_factory=factory))

    assert status.ok is True
    uow = factory.created[0]
    assert uow.conversations.calls, "репозиторий должен был быть вызван"


async def test_database_failure_is_reported_by_exception_type() -> None:
    """Наружу уходит только тип исключения, без DSN и текста драйвера."""
    status = await check_database(make_container(uow_factory=FakeUowFactory(session_broken=True)))

    assert status.ok is False
    assert status.detail == "RuntimeError"
    assert status.critical is True


# --------------------------------------------------------------------------- #
# Redis
# --------------------------------------------------------------------------- #


def test_redis_is_not_required_for_inline_queue() -> None:
    """В режиме ``inline`` Redis не нужен: кэш и лимиты живут в процессе."""
    assert redis_required(make_container(make_settings())) is False


def test_redis_is_required_for_arq_queue() -> None:
    """С брокером ``arq`` без Redis воркер не получит ни одной задачи."""
    settings = make_settings(queue={"backend": "arq"}, redis={"dsn": "redis://127.0.0.1:1/0"})
    assert redis_required(make_container(settings)) is True


async def test_redis_check_is_skipped_when_not_required() -> None:
    """Необязательный Redis не проверяется сетью и не блокирует готовность."""
    status = await check_redis(make_container(make_settings()))

    assert status.ok is True
    assert status.detail == "not_required"
    assert status.critical is False


async def test_redis_failure_is_critical_when_required() -> None:
    """Недоступный брокер задач — критическая проверка."""
    settings = make_settings(
        queue={"backend": "arq"},
        redis={"dsn": "redis://127.0.0.1:1/0"},
    )
    status = await check_redis(make_container(settings), timeout_s=0.5)

    assert status.ok is False
    assert status.critical is True
    # Порт 1 на loopback: либо отказ соединения, либо таймаут/запрет сети.
    assert status.detail is not None


async def test_redis_invalid_dsn_is_reported() -> None:
    """Некорректный DSN — ошибка конфигурации, а не сетевой таймаут."""
    settings = make_settings(redis={"dsn": "not-a-dsn://", "required": True})
    status = await check_redis(make_container(settings), timeout_s=0.5)

    assert status.ok is False
    assert status.detail is not None
    assert status.detail.startswith("invalid_dsn:")


# --------------------------------------------------------------------------- #
# Каналы
# --------------------------------------------------------------------------- #


async def test_no_channels_is_a_valid_configuration() -> None:
    """Демо на fake-адаптерах без площадок: сервис готов."""
    statuses = await check_channels(make_container(channels={}))

    names = [status.name for status in statuses]
    assert names == ["channels"]
    assert statuses[0].ok is True
    assert statuses[0].detail is None


async def test_healthy_and_unhealthy_channels_are_listed_separately() -> None:
    """По каждому включённому каналу — отдельная строка отчёта."""
    container = make_container(
        channels={
            Channel.HH: FakeChannel(Channel.HH, healthy=True),
            Channel.AVITO: FakeChannel(Channel.AVITO, healthy=False),
        }
    )
    statuses = await check_channels(container)

    by_name = {status.name: status for status in statuses}
    assert by_name[f"{CHANNEL_PREFIX}hh"].ok is True
    assert by_name[f"{CHANNEL_PREFIX}avito"].ok is False
    # Один живой канал из двух — сервис готов.
    assert by_name["channels"].ok is True
    assert by_name["channels"].detail == "1/2"


async def test_all_channels_down_blocks_readiness() -> None:
    """Все каналы недоступны — трафик принимать нельзя."""
    container = make_container(channels={Channel.HH: FakeChannel(Channel.HH, healthy=False)})
    statuses = await check_channels(container)

    aggregate = next(status for status in statuses if status.name == "channels")
    assert aggregate.ok is False
    assert aggregate.critical is True


async def test_channel_exception_is_recorded_by_type() -> None:
    """Исключение адаптера не выходит наружу: только имя класса."""
    channel = FakeChannel(Channel.HH, error=ConnectionError("площадка недоступна"))
    statuses = await check_channels(make_container(channels={Channel.HH: channel}))

    assert statuses[0].ok is False
    assert statuses[0].detail == "ConnectionError"


async def test_channel_healthcheck_is_bounded_by_timeout() -> None:
    """Зависший healthcheck не вешает пробу: срабатывает таймаут."""

    class HangingChannel(FakeChannel):
        async def healthcheck(self) -> bool:
            await asyncio.sleep(30)
            return True

    statuses = await check_channels(make_container(channels={Channel.HH: HangingChannel()}), timeout_s=0.01)

    assert statuses[0].ok is False
    assert statuses[0].detail == "TimeoutError"


# --------------------------------------------------------------------------- #
# Календарь и LLM
# --------------------------------------------------------------------------- #


async def test_calendar_not_configured_is_not_a_failure() -> None:
    """Без календаря нельзя только бронировать — это не отказ готовности."""
    status = await check_calendar(make_container(calendar=None))

    assert status.ok is True
    assert status.detail == "not_configured"
    assert status.critical is False


async def test_calendar_healthcheck_is_used() -> None:
    """Если адаптер умеет healthcheck — он и вызывается."""
    calendar = FakeCalendar(healthy=False)
    status = await check_calendar(make_container(calendar=calendar))

    assert calendar.healthchecks == 1
    assert status.ok is False
    assert status.detail is None


async def test_calendar_without_healthcheck_is_reported_honestly() -> None:
    """Адаптер без healthcheck не притворяется проверенным."""
    status = await check_calendar(make_container(calendar=CalendarWithoutHealthcheck()))

    assert status.ok is True
    assert status.detail == "healthcheck_not_supported"


async def test_fake_llm_needs_no_probe() -> None:
    """Провайдер ``fake`` — детерминированная заглушка, проверять нечего."""
    status = await check_llm(make_container())

    assert status.ok is True
    assert status.detail == "fake"


async def test_paid_llm_without_api_key_is_degraded_but_not_critical() -> None:
    """Без ключа диалоги уходят в HUMAN_REVIEW, но сервис работает."""
    settings = make_settings(llm={"provider": "openai_compat"})
    status = await check_llm(make_container(settings, llm=FakeLlmWithHealthcheck()))

    assert status.ok is False
    assert status.detail == "api_key_missing"
    assert status.critical is False


async def test_paid_llm_uses_adapter_healthcheck() -> None:
    """С ключом и healthcheck у адаптера проверяется реальная доступность."""
    settings = make_settings(llm={"provider": "openai_compat", "api_key": "sk-test"})
    llm = FakeLlmWithHealthcheck(healthy=False)
    status = await check_llm(make_container(settings, llm=llm))

    assert llm.healthchecks == 1
    assert status.ok is False
    assert status.detail == "openai_compat"


# --------------------------------------------------------------------------- #
# Полный отчёт
# --------------------------------------------------------------------------- #


async def test_full_report_of_healthy_service() -> None:
    """Полностью здоровая конфигурация: ready и предсказуемый порядок проверок."""
    container = make_container(
        channels={Channel.HH: FakeChannel(Channel.HH)},
        calendar=FakeCalendar(),
    )
    report = await check_dependencies(container)

    assert report.is_ready is True
    assert report.status == "ready"
    assert [check.name for check in report.checks] == [
        "database",
        "redis",
        "channel:hh",
        "channels",
        "calendar",
        "llm",
    ]
    assert report.failed == ()
    assert report.database.ok is True
    assert [check.name for check in report.channels] == ["channel:hh"]


async def test_report_is_degraded_when_database_is_down() -> None:
    """БД — критическая зависимость."""
    container = make_container(uow_factory=FakeUowFactory(session_broken=True))
    report = await check_dependencies(container)

    assert report.is_ready is False
    assert report.status == "degraded"
    assert [check.name for check in report.failed] == ["database"]


async def test_report_stays_ready_when_only_calendar_is_down() -> None:
    """Упавший календарь виден в отчёте, но трафик не блокирует."""
    container = make_container(calendar=FakeCalendar(error=TimeoutError("таймаут")))
    report = await check_dependencies(container)

    assert report.is_ready is True
    assert [check.name for check in report.failed] == ["calendar"]
    assert report.calendar is not None
    assert report.calendar.detail == "TimeoutError"


async def test_report_is_json_serializable() -> None:
    """Отчёт можно положить в лог и в диагностический ответ."""
    report = await check_dependencies(make_container())

    payload = json.dumps(report.as_dict(), ensure_ascii=False)

    assert '"status": "ready"' in payload
    parsed = json.loads(payload)
    assert parsed["checks"][0]["name"] == "database"
    assert "critical" in parsed["checks"][0]


async def test_component_view_matches_ready_response_schema() -> None:
    """``as_component`` даёт поля, совместимые с ``ComponentHealth``."""
    report = await check_dependencies(make_container(channels={Channel.HH: FakeChannel(Channel.HH)}))

    database = report.database.as_component()
    channels = [check.as_component() for check in report.channels]

    assert database == {"name": "database", "ok": True, "detail": None}
    assert channels == [{"name": "channel:hh", "ok": True, "detail": None}]


def test_report_without_database_is_a_programming_error() -> None:
    """Инвариант отчёта: БД в нём всегда есть."""
    report = ReadinessReport(checks=(DependencyStatus(name="redis", ok=True),))

    with pytest.raises(RuntimeError):
        _ = report.database
