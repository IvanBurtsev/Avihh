"""Заглушки для тестов метрик и readiness.

Модуль лежит рядом с тестами (как ``api_support`` у тестов API) и даёт:

* ``make_settings`` — настройки тестового окружения (без чтения ``.env``);
* ``make_container`` — ``Container`` на заглушках портов: без сети, БД и Redis;
* ``FakeUowFactory`` — Unit of Work с управляемыми отказами (broken session,
  broken repository, отсутствие ``session``);
* ``FakeChannel`` / ``FakeCalendar`` / ``FakeLlm`` — зависимости readiness;
* ``sample_value`` / ``metric_samples`` — разбор текста экспозиции Prometheus:
  тесты проверяют **публичный контракт** (имена и метки метрик), а не
  внутренние объекты библиотеки.
"""

from __future__ import annotations

import re
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from hirebot.api import routes_metrics
from hirebot.config import Settings
from hirebot.container import Container
from hirebot.domain.enums import Channel, ConversationState
from hirebot.observability.metrics import HttpMetricsMiddleware

# --------------------------------------------------------------------------- #
# Настройки и контейнер
# --------------------------------------------------------------------------- #


def make_settings(**overrides: Any) -> Settings:
    """Настройки теста: локальное окружение, без чтения файла ``.env``."""
    base: dict[str, Any] = {
        "env": "test",
        "database": {"dsn": "sqlite+aiosqlite:///:memory:"},
        "redis": {"dsn": "redis://127.0.0.1:1/0", "required": False},
        "queue": {"backend": "inline"},
        "llm": {"provider": "fake"},
    }
    base.update(overrides)
    return Settings(_env_file=None, **base)


class FakeSession:
    """Сессия БД: ``execute`` падает, если сконфигурирован отказ."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.statements: list[str] = []

    async def execute(self, statement: object) -> None:
        if self.broken:
            msg = "БД недоступна (fake)"
            raise RuntimeError(msg)
        self.statements.append(str(statement))


class FakeConversationsRepository:
    """Репозиторий диалогов: используется, когда у UoW нет ``session``."""

    def __init__(self, *, broken: bool = False) -> None:
        self.broken = broken
        self.calls: list[tuple[Sequence[ConversationState], int]] = []

    async def list_by_state(
        self, states: Sequence[ConversationState], *, limit: int = 100
    ) -> Sequence[object]:
        if self.broken:
            msg = "таблица conversations недоступна (fake)"
            raise RuntimeError(msg)
        self.calls.append((tuple(states), limit))
        return ()


class FakeUnitOfWork:
    """Unit of Work с асинхронным контекстом (как в ``hirebot.ports``)."""

    def __init__(
        self,
        *,
        session: FakeSession | None,
        conversations: FakeConversationsRepository,
    ) -> None:
        self.session = session
        self.conversations = conversations
        self.committed = False

    async def commit(self) -> None:
        self.committed = True

    async def rollback(self) -> None:
        return None


class FakeUowFactory:
    """Фабрика Unit of Work: каждый вызов даёт новую «транзакцию»."""

    def __init__(
        self,
        *,
        session_broken: bool = False,
        repo_broken: bool = False,
        with_session: bool = True,
    ) -> None:
        self.session_broken = session_broken
        self.repo_broken = repo_broken
        self.with_session = with_session
        self.created: list[FakeUnitOfWork] = []

    @asynccontextmanager
    async def __call__(self) -> AsyncIterator[FakeUnitOfWork]:
        uow = FakeUnitOfWork(
            session=FakeSession(broken=self.session_broken) if self.with_session else None,
            conversations=FakeConversationsRepository(broken=self.repo_broken),
        )
        self.created.append(uow)
        yield uow


class _StubPort:
    """Заглушка порта: любой вызов падает, поход в сеть невозможен."""

    async def aclose(self) -> None:
        return None

    async def __getattr__(self, name: str) -> Callable[..., Any]:
        def _fail(*_args: Any, **_kwargs: Any) -> Any:
            msg = f"unit-тест observability не должен вызывать {name}()"
            raise AssertionError(msg)

        return _fail


class FakeChannel:
    """Канал площадки: ``healthcheck`` возвращает заданный результат."""

    def __init__(
        self,
        channel: Channel = Channel.HH,
        *,
        healthy: bool = True,
        error: Exception | None = None,
    ) -> None:
        self.channel = channel
        self.healthy = healthy
        self.error = error
        self.healthchecks = 0

    async def healthcheck(self) -> bool:
        self.healthchecks += 1
        if self.error is not None:
            raise self.error
        return self.healthy


class FakeCalendar:
    """Календарь с ``healthcheck`` (как у Talantix)."""

    def __init__(self, *, healthy: bool = True, error: Exception | None = None) -> None:
        self.healthy = healthy
        self.error = error
        self.healthchecks = 0

    async def healthcheck(self) -> bool:
        self.healthchecks += 1
        if self.error is not None:
            raise self.error
        return self.healthy


class CalendarWithoutHealthcheck:
    """Календарь без ``healthcheck``: проба должна честно это сообщить."""


class FakeLlm:
    """LLM-адаптер без ``healthcheck`` (fake-провайдер)."""


class FakeLlmWithHealthcheck:
    """LLM-адаптер с ``healthcheck`` — как у будущих боевых провайдеров."""

    def __init__(self, *, healthy: bool = True) -> None:
        self.healthy = healthy
        self.healthchecks = 0

    async def healthcheck(self) -> bool:
        self.healthchecks += 1
        return self.healthy


def make_container(
    settings: Settings | None = None,
    *,
    uow_factory: FakeUowFactory | None = None,
    channels: Mapping[Channel, Any] | None = None,
    calendar: Any | None = None,
    llm: Any | None = None,
) -> Container:
    """Собрать контейнер на заглушках."""
    stub = _StubPort()
    return Container(
        settings=settings or make_settings(),
        uow_factory=uow_factory or FakeUowFactory(),
        http=stub,
        cache=stub,
        locks=stub,
        rate_limiter=stub,
        queue=stub,
        clock=stub,
        llm=llm if llm is not None else FakeLlm(),
        cipher=object(),
        channels=dict(channels or {}),
        calendar=calendar,
    )


# --------------------------------------------------------------------------- #
# HTTP-клиент и приложение для проверки middleware и роутера
# --------------------------------------------------------------------------- #


def build_metrics_app(
    container: Container,
    *,
    with_middleware: bool = True,
    with_router: bool = True,
) -> FastAPI:
    """Приложение минимального состава: только то, что нужно для метрик.

    ``create_app`` из ``hirebot.api.app`` здесь не используется намеренно:
    тест не должен зависеть от порядка подключения роутеров и от контейнера,
    который собирает ``lifespan``.
    """

    async def item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    async def boom() -> dict[str, str]:
        msg = "падение обработчика"
        raise RuntimeError(msg)

    app = FastAPI()
    app.state.container = container
    if with_middleware:
        app.add_middleware(HttpMetricsMiddleware)
    if with_router:
        app.include_router(routes_metrics.router)
    app.get("/items/{item_id}")(item)
    app.get("/boom")(boom)
    return app


def client_for(app: FastAPI) -> AsyncClient:
    """HTTP-клиент поверх ASGI-транспорта (без сети и портов)."""
    return AsyncClient(transport=ASGITransport(app=app), base_url="http://testserver")


# --------------------------------------------------------------------------- #
# Разбор экспозиции Prometheus
# --------------------------------------------------------------------------- #

_SAMPLE_RE = re.compile(r"^(?P<name>[a-zA-Z_:][a-zA-Z0-9_:]*)(?:\{(?P<labels>.*)\})?\s+(?P<value>\S+)$")
_LABEL_RE = re.compile(r'([a-zA-Z_][a-zA-Z0-9_]*)="((?:[^"\\]|\\.)*)"')


def metric_samples(payload: str) -> list[tuple[str, dict[str, str], float]]:
    """Распарсить текст экспозиции в список ``(имя, метки, значение)``.

    Разбор нарочно простой, но обязан выдерживать фигурные скобки внутри
    значений меток: шаблон маршрута ``/items/{item_id}`` — обычное дело.
    """
    samples: list[tuple[str, dict[str, str], float]] = []
    for raw_line in payload.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        match = _SAMPLE_RE.match(line)
        if match is None:
            continue
        labels = dict(_LABEL_RE.findall(match.group("labels") or ""))
        samples.append((match.group("name"), labels, float(match.group("value"))))
    return samples


def sample_value(payload: str, name: str, **labels: str) -> float | None:
    """Значение сэмпла по имени и меткам (``None`` — если серии нет).

    Метки сравниваются по подмножеству: у гистограмм в имени уже есть
    ``_bucket``/``_count``/``_sum``, и часть меток (например ``le``) передаётся
    явно.
    """
    for sample_name, sample_labels, value in metric_samples(payload):
        if sample_name != name:
            continue
        if all(sample_labels.get(key) == expected for key, expected in labels.items()):
            return value
    return None
