"""Расширенная проверка готовности: БД, Redis, каналы, календарь, LLM.

Зачем отдельный модуль, если ``/health/ready`` уже есть
------------------------------------------------------
Прослойке и дежурному нужен один ответ на вопрос «можно ли слать трафик», а
не пять разных эндпоинтов. При этом проверки должны:

* **не валить сервис из-за необязательной зависимости**: недоступный
  календарь или LLM — это деградация (диалоги уходят в ``HUMAN_REVIEW``), а не
  причина выводить инстанс из балансировки;
* **не вешать пробу**: каждая проверка ограничена таймаутом, иначе зависший
  сокет к площадке делает ``/health/ready`` вечно «в процессе» и Kubernetes
  считает инстанс живым;
* **не отдавать наружу лишнего**: в деталях только имя зависимости и тип
  исключения — ни DSN, ни текстов ошибок драйверов, ни персональных данных.

Модуль ничего не регистрирует в приложении сам: роутер ``routes_health``
остаётся единственным местом, где решается, как отчёт превращается в HTTP-ответ.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass
from typing import Final, Literal

from sqlalchemy import text

from hirebot.container import Container
from hirebot.domain.enums import ConversationState
from hirebot.infra.redis_client import close_redis_client, create_redis_client
from hirebot.logging_setup import get_logger

logger = get_logger(__name__)

#: Таймаут одной проверки по умолчанию. Пробу редко дают больше 5 секунд,
#: поэтому даже полный отчёт с недоступной внешней системой укладывается в лимит.
DEFAULT_CHECK_TIMEOUT_S: Final = 2.0

#: Префикс имён проверок каналов (совпадает с ``/health/ready``).
CHANNEL_PREFIX: Final = "channel:"

ReadyStatus = Literal["ready", "degraded"]


@dataclass(slots=True, frozen=True)
class DependencyStatus:
    """Результат проверки одной зависимости.

    ``critical=True`` означает, что при отказе сервис не должен принимать
    трафик. Всё остальное — деградация: сервис работает, но с ограничениями.
    """

    name: str
    ok: bool
    detail: str | None = None
    critical: bool = False

    def as_component(self) -> dict[str, object]:
        """Представление в формате ``hirebot.api.schemas.ComponentHealth``."""
        return {"name": self.name, "ok": self.ok, "detail": self.detail}

    def as_dict(self) -> dict[str, object]:
        """Полное представление для логов и диагностических ответов."""
        return {
            "name": self.name,
            "ok": self.ok,
            "detail": self.detail,
            "critical": self.critical,
        }


@dataclass(slots=True, frozen=True)
class ReadinessReport:
    """Структурированный отчёт о готовности сервиса.

    ``is_ready`` учитывает только критические проверки: один живой канал из
    двух, недоступный календарь и выключенная LLM трафик не блокируют.
    """

    checks: tuple[DependencyStatus, ...]

    @property
    def is_ready(self) -> bool:
        """Можно ли принимать трафик."""
        return all(check.ok for check in self.checks if check.critical)

    @property
    def status(self) -> ReadyStatus:
        """Словесный статус для ``/health/ready``."""
        return "ready" if self.is_ready else "degraded"

    @property
    def failed(self) -> tuple[DependencyStatus, ...]:
        """Проверки, которые завершились отказом (включая некритические)."""
        return tuple(check for check in self.checks if not check.ok)

    def get(self, name: str) -> DependencyStatus | None:
        """Найти проверку по имени."""
        return next((check for check in self.checks if check.name == name), None)

    @property
    def database(self) -> DependencyStatus:
        """Проверка БД: она в отчёте всегда есть."""
        found = self.get("database")
        if found is None:  # pragma: no cover - инвариант сборки отчёта
            msg = "Отчёт готовности собран без проверки БД"
            raise RuntimeError(msg)
        return found

    @property
    def channels(self) -> tuple[DependencyStatus, ...]:
        """Проверки каналов (без агрегата ``channels``)."""
        return tuple(check for check in self.checks if check.name.startswith(CHANNEL_PREFIX))

    @property
    def redis(self) -> DependencyStatus | None:
        """Проверка Redis (``None`` — если её не было в отчёте)."""
        return self.get("redis")

    @property
    def calendar(self) -> DependencyStatus | None:
        """Проверка календаря."""
        return self.get("calendar")

    @property
    def llm(self) -> DependencyStatus | None:
        """Проверка LLM-провайдера."""
        return self.get("llm")

    def as_dict(self) -> dict[str, object]:
        """Отчёт в виде, пригодном для ``json.dumps`` и логов."""
        return {
            "status": self.status,
            "ready": self.is_ready,
            "checks": [check.as_dict() for check in self.checks],
        }


def _joined(names: Iterable[str]) -> str:
    """Имена отказавших проверок через запятую — для строки лога."""
    return ",".join(names) or "none"


async def check_database(
    container: Container, *, timeout_s: float = DEFAULT_CHECK_TIMEOUT_S
) -> DependencyStatus:
    """Проверить БД дешёвым запросом ``SELECT 1``.

    Соединение проверяется, схема — нет: отсутствие миграций должен показывать
    деплой (``alembic check``), а не readiness, иначе после каждого релиза
    инстанс выпадал бы из балансировки раньше, чем применятся миграции.

    Если у Unit of Work нет ``session`` (fake-контейнер в тестах и демо),
    используется чтение через репозиторий — тогда проверка означает «доступны и
    соединение, и таблицы».
    """
    try:
        async with asyncio.timeout(timeout_s):
            async with container.uow_factory() as uow:
                execute = getattr(getattr(uow, "session", None), "execute", None)
                if callable(execute):
                    await execute(text("SELECT 1"))
                else:
                    await uow.conversations.list_by_state((ConversationState.NEW,), limit=1)
    except Exception as exc:
        logger.warning("readiness_database_failed", error_type=type(exc).__name__)
        return DependencyStatus(name="database", ok=False, detail=type(exc).__name__, critical=True)
    return DependencyStatus(name="database", ok=True, critical=True)


def redis_required(container: Container) -> bool:
    """Нужен ли Redis этой конфигурации.

    Redis обязателен, если так задано в настройках или если брокер задач —
    ``arq`` (без Redis воркер не получит ни одной задачи, и воронка встанет).
    В режимах ``inline``/demo Redis необязателен: кэш, локи и лимиты
    деградируют до in-process (ARCHITECTURE.md, п. 2.6).
    """
    settings = container.settings
    return settings.redis.required or settings.queue.backend == "arq"


async def check_redis(
    container: Container, *, timeout_s: float = DEFAULT_CHECK_TIMEOUT_S
) -> DependencyStatus:
    """Проверить Redis, если он нужен текущей конфигурации.

    Клиент создаётся на один ``PING`` и сразу закрывается: readiness вызывается
    часто, и держать отдельный пул ради пробы незачем.
    """
    required = redis_required(container)
    if not required:
        return DependencyStatus(name="redis", ok=True, detail="not_required", critical=False)

    try:
        client = create_redis_client(container.settings)
    except Exception as exc:  # некорректный DSN — ошибка конфигурации, а не сети
        logger.warning("readiness_redis_dsn_invalid", error_type=type(exc).__name__)
        return DependencyStatus(
            name="redis", ok=False, detail=f"invalid_dsn:{type(exc).__name__}", critical=True
        )

    try:
        async with asyncio.timeout(timeout_s):
            await client.ping()
    except Exception as exc:
        logger.warning("readiness_redis_failed", error_type=type(exc).__name__)
        return DependencyStatus(name="redis", ok=False, detail=type(exc).__name__, critical=True)
    finally:
        await close_redis_client(client)
    return DependencyStatus(name="redis", ok=True, critical=True)


async def check_channels(
    container: Container, *, timeout_s: float = DEFAULT_CHECK_TIMEOUT_S
) -> list[DependencyStatus]:
    """Проверить включённые каналы их собственным ``healthcheck``.

    Возвращает по одной проверке на канал (некритические: для готовности важен
    агрегат) и агрегат ``channels``, который критичен, если не жив ни один
    канал. Если каналов нет вовсе — это валидная конфигурация (демо и
    fake-адаптеры), и сервис готов.
    """
    results: list[DependencyStatus] = []
    for channel, adapter in container.channels.items():
        name = f"{CHANNEL_PREFIX}{channel.value}"
        try:
            async with asyncio.timeout(timeout_s):
                healthy = bool(await adapter.healthcheck())
        except Exception as exc:
            logger.warning("readiness_channel_failed", channel=channel.value, error_type=type(exc).__name__)
            results.append(DependencyStatus(name=name, ok=False, detail=type(exc).__name__))
            continue
        results.append(DependencyStatus(name=name, ok=healthy))

    total = len(results)
    healthy_count = sum(1 for result in results if result.ok)
    aggregate_ok = total == 0 or healthy_count > 0
    results.append(
        DependencyStatus(
            name="channels",
            ok=aggregate_ok,
            detail=None if total == 0 else f"{healthy_count}/{total}",
            critical=True,
        )
    )
    return results


async def check_calendar(
    container: Container, *, timeout_s: float = DEFAULT_CHECK_TIMEOUT_S
) -> DependencyStatus:
    """Проверить календарь, если он сконфигурирован.

    Используется ``healthcheck`` адаптера (``GET /auth_check`` у Talantix):
    он проверяет токен и не создаёт встреч. Проверка некритична — без
    календаря нельзя только бронировать, диалог продолжается.
    """
    calendar = container.calendar
    if calendar is None:
        return DependencyStatus(name="calendar", ok=True, detail="not_configured")

    healthcheck = getattr(calendar, "healthcheck", None)
    if not callable(healthcheck):
        # Адаптер без healthcheck: проверять нечем, и делать пробную запись
        # ради пробы недопустимо.
        return DependencyStatus(name="calendar", ok=True, detail="healthcheck_not_supported")
    try:
        async with asyncio.timeout(timeout_s):
            healthy = bool(await healthcheck())
    except Exception as exc:
        logger.warning("readiness_calendar_failed", error_type=type(exc).__name__)
        return DependencyStatus(name="calendar", ok=False, detail=type(exc).__name__)
    return DependencyStatus(name="calendar", ok=healthy)


async def check_llm(container: Container, *, timeout_s: float = DEFAULT_CHECK_TIMEOUT_S) -> DependencyStatus:
    """Проверить LLM-провайдера.

    Пробный вызов модели не делается: он стоит денег и искажает метрики.
    Проверяется конфигурация (для платного провайдера нужен ключ) и, если
    адаптер умеет, его собственный ``healthcheck``. Проверка некритична:
    при недоступной модели диалоги уходят в ``HUMAN_REVIEW``, а не теряются.
    """
    settings = container.settings
    provider = settings.llm.provider
    if provider == "fake":
        return DependencyStatus(name="llm", ok=True, detail="fake")

    if settings.llm.api_key is None:
        # Не падаем: без ключа сервис работает, но каждый диалог требует человека.
        logger.warning("readiness_llm_key_missing", provider=provider)
        return DependencyStatus(name="llm", ok=False, detail="api_key_missing")

    healthcheck = getattr(container.llm, "healthcheck", None)
    if not callable(healthcheck):
        return DependencyStatus(name="llm", ok=True, detail=f"{provider}:configured")
    try:
        async with asyncio.timeout(timeout_s):
            healthy = bool(await healthcheck())
    except Exception as exc:
        logger.warning("readiness_llm_failed", provider=provider, error_type=type(exc).__name__)
        return DependencyStatus(name="llm", ok=False, detail=type(exc).__name__)
    return DependencyStatus(name="llm", ok=healthy, detail=provider)


async def check_dependencies(
    container: Container,
    *,
    timeout_s: float = DEFAULT_CHECK_TIMEOUT_S,
) -> ReadinessReport:
    """Собрать отчёт о готовности по всем зависимостям.

    Порядок проверок фиксирован (БД, Redis, каналы, календарь, LLM), чтобы
    отчёт было удобно читать и сравнивать между запусками. Сами проверки
    выполняются параллельно: у каждой свой таймаут, и общий ответ не должен
    складываться из задержек всех внешних систем.
    """
    database, redis, channels, calendar, llm = await asyncio.gather(
        check_database(container, timeout_s=timeout_s),
        check_redis(container, timeout_s=timeout_s),
        check_channels(container, timeout_s=timeout_s),
        check_calendar(container, timeout_s=timeout_s),
        check_llm(container, timeout_s=timeout_s),
    )

    checks: list[DependencyStatus] = [database, redis, *channels, calendar, llm]
    report = ReadinessReport(checks=tuple(checks))
    if not report.is_ready:
        logger.warning(
            "readiness_degraded",
            failed=_joined(check.name for check in report.failed),
            critical_failed=_joined(check.name for check in report.failed if check.critical),
        )
    return report


__all__ = [
    "CHANNEL_PREFIX",
    "DEFAULT_CHECK_TIMEOUT_S",
    "DependencyStatus",
    "ReadinessReport",
    "ReadyStatus",
    "check_calendar",
    "check_channels",
    "check_database",
    "check_dependencies",
    "check_llm",
    "check_redis",
    "redis_required",
]
