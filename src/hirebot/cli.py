"""CLI hirebot (Typer + Rich).

Команды:

* ``hirebot doctor`` — проверка конфигурации и доступности зависимостей;
* ``hirebot db init`` — создать схему БД;
* ``hirebot poll --channel hh --once`` — поллинг площадки;
* ``hirebot serve`` — HTTP API (uvicorn);
* ``hirebot worker [--inline]`` — фоновый воркер.

Вывод команд рассчитан на демонстрацию: статусы цветные, у каждой проверки
видна причина, секреты и DSN с паролем в отчёт не попадают.
"""

from __future__ import annotations

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass
from importlib import import_module
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit, urlunsplit

import typer
from rich.console import Console
from rich.table import Table

from hirebot import __version__
from hirebot.config import Settings, get_settings
from hirebot.container import build_container
from hirebot.domain.enums import Channel, ConversationState
from hirebot.logging_setup import configure_logging, ensure_utf8_streams, get_logger

app = typer.Typer(
    name="hirebot",
    help="AI-рекрутер: отклики с площадок -> скрининг -> диалог -> собеседование.",
    no_args_is_help=True,
    add_completion=False,
)
db_app = typer.Typer(name="db", help="Работа со схемой БД.", no_args_is_help=True)
app.add_typer(db_app, name="db")

console = Console()
err_console = Console(stderr=True)
logger = get_logger(__name__)

Status = Literal["ok", "warn", "fail", "skip"]

#: Размер пачки outbox за один проход inline-воркера.
INLINE_BATCH_SIZE = 20

#: Эталонная выборка для замера качества скрининга (относительно корня проекта).
DEFAULT_DATASET_PATH = "data/evaluation/golden_set.json"

#: Справочник критериев подбора (относительно корня проекта).
DEFAULT_CATALOG_PATH = "data/criteria/catalog.yaml"

#: Цвет и подпись для каждого статуса проверки.
_STATUS_STYLE: dict[Status, tuple[str, str]] = {
    "ok": ("green", "OK"),
    "warn": ("yellow", "ВНИМАНИЕ"),
    "fail": ("red", "ОШИБКА"),
    "skip": ("dim", "ПРОПУЩЕНО"),
}


# ---------------------------------------------------------------------------
# Общие помощники
# ---------------------------------------------------------------------------


def _run_async[T](coro: Coroutine[Any, Any, T]) -> T:
    """Запустить корутину и корректно отреагировать на Ctrl+C."""
    try:
        return asyncio.run(coro)
    except KeyboardInterrupt:
        err_console.print("[yellow]Остановлено пользователем[/yellow]")
        raise typer.Exit(code=130) from None


def _redact_dsn(dsn: str) -> str:
    """Убрать из DSN пароль: отчёт doctor попадает в скриншоты и тикеты."""
    parts = urlsplit(dsn)
    if parts.password is None:
        return dsn
    host = parts.hostname or ""
    if parts.port is not None:
        host = f"{host}:{parts.port}"
    return urlunsplit((parts.scheme, f"***:***@{host}", parts.path, "", ""))


def _load_settings() -> Settings:
    """Прочитать настройки, объяснив ошибку конфигурации по-человечески."""
    try:
        return get_settings()
    except Exception as exc:
        err_console.print(f"[red]Некорректная конфигурация:[/red] {exc}")
        raise typer.Exit(code=2) from exc


# ---------------------------------------------------------------------------
# doctor
# ---------------------------------------------------------------------------


@dataclass(slots=True, frozen=True)
class Check:
    """Результат одной проверки конфигурации."""

    title: str
    status: Status
    detail: str
    critical: bool = False


def _channels_check(settings: Settings) -> Check:
    enabled = [
        name for name, flag in (("hh", settings.hh.enabled), ("avito", settings.avito.enabled)) if flag
    ]
    if not enabled:
        return Check(
            "Каналы площадок",
            "warn",
            "ни один канал не включён (HIREBOT_HH__ENABLED / HIREBOT_AVITO__ENABLED) — поллить нечего",
        )
    return Check("Каналы площадок", "ok", "включены: " + ", ".join(enabled))


def _llm_check(settings: Settings) -> Check:
    provider = settings.llm.provider
    if provider == "fake":
        return Check(
            "LLM-провайдер",
            "warn",
            "fake: детерминированные ответы без сети и ключей (режим демо и тестов)",
        )
    if settings.llm.api_key is None:
        return Check(
            "LLM-провайдер",
            "fail",
            f"{provider}: не задан HIREBOT_LLM__API_KEY",
            critical=True,
        )
    return Check("LLM-провайдер", "ok", f"{provider}, модель {settings.llm.model}")


def _calendar_check(settings: Settings) -> Check:
    if not settings.talantix.enabled:
        return Check(
            "Календарь Talantix",
            "warn",
            "выключен: автобронирование собеседований недоступно (HIREBOT_TALANTIX__ENABLED=true)",
        )
    if settings.talantix.access_token is None:
        return Check(
            "Календарь Talantix",
            "fail",
            "включён, но не задан HIREBOT_TALANTIX__ACCESS_TOKEN",
            critical=True,
        )
    return Check(
        "Календарь Talantix",
        "ok",
        f"окно выборки {settings.talantix.max_window_days} дн., {settings.talantix.default_timezone}",
    )


def _pii_check(settings: Settings) -> Check:
    if settings.security.pii_key is not None:
        return Check("Шифрование ПДн", "ok", "ключ Fernet задан")
    if settings.is_local:
        return Check(
            "Шифрование ПДн",
            "warn",
            "ключ не задан: допустимо только локально, данные сохраняются без шифрования",
        )
    return Check(
        "Шифрование ПДн",
        "fail",
        "не задан HIREBOT_SECURITY__PII_KEY (обязателен вне local/test)",
        critical=True,
    )


def _queue_check(settings: Settings) -> Check:
    backend = settings.queue.backend
    if backend == "arq":
        return Check("Очередь задач", "ok", f"arq, очередь {settings.queue.queue_name}")
    return Check(
        "Очередь задач",
        "warn",
        "inline: задачи выполняются в процессе API (демо-режим; для продакшена HIREBOT_QUEUE__BACKEND=arq)",
    )


async def _database_check(settings: Settings) -> Check:
    """Проверить БД: соединение и (по возможности) наличие схемы."""
    try:
        session_module = import_module("hirebot.infra.db.session")
        create_uow_factory = session_module.create_uow_factory
    except (ImportError, AttributeError) as exc:
        return Check("База данных", "skip", f"инфраструктура БД недоступна: {exc}")
    try:
        factory = create_uow_factory(settings)
        async with factory() as uow:
            execute = getattr(getattr(uow, "session", None), "execute", None)
            if callable(execute):
                from sqlalchemy import text

                await execute(text("SELECT 1"))
            else:
                await uow.conversations.list_by_state((ConversationState.NEW,), limit=1)
    except Exception as exc:
        return Check(
            "База данных",
            "fail",
            f"недоступна ({type(exc).__name__}); проверьте HIREBOT_DATABASE__DSN "
            f"({_redact_dsn(settings.database.dsn)})",
            critical=True,
        )
    return Check("База данных", "ok", f"соединение установлено ({_redact_dsn(settings.database.dsn)})")


async def _redis_check(settings: Settings) -> Check:
    """Проверить Redis: без него деградируют кэш, локи и rate limit."""
    required = settings.redis.required or settings.queue.backend == "arq"
    try:
        from redis.asyncio import Redis
    except ImportError as exc:
        return Check("Redis", "skip", f"клиент redis недоступен: {exc}")
    try:
        client = Redis.from_url(settings.redis.dsn, socket_connect_timeout=2.0, socket_timeout=2.0)
    except Exception as exc:
        return Check("Redis", "fail", f"некорректный DSN ({type(exc).__name__})", critical=required)
    try:
        await client.ping()
    except Exception as exc:
        detail = f"недоступен ({type(exc).__name__})"
        if required:
            return Check("Redis", "fail", f"{detail}; он обязателен для этой конфигурации", critical=True)
        return Check("Redis", "warn", f"{detail}; кэш, локи и лимиты деградируют до in-process")
    finally:
        await client.aclose()
    return Check("Redis", "ok", f"ping прошёл ({_redact_dsn(settings.redis.dsn)})")


async def _collect_checks(settings: Settings) -> list[Check]:
    """Собрать полный отчёт о конфигурации."""
    return [
        Check(
            "Окружение",
            "ok",
            f"env={settings.env}, уровень логов={settings.log_level}, JSON-логи={settings.log_json}",
        ),
        _channels_check(settings),
        await _database_check(settings),
        await _redis_check(settings),
        _calendar_check(settings),
        _llm_check(settings),
        _queue_check(settings),
        _pii_check(settings),
    ]


def _render_checks(checks: list[Check], settings: Settings) -> bool:
    """Печать отчёта; возвращает ``True``, если есть критичные проблемы."""
    table = Table(title=f"hirebot doctor (v{__version__})", show_lines=False)
    table.add_column("Проверка", style="bold", no_wrap=True)
    table.add_column("Статус", no_wrap=True)
    table.add_column("Детали")
    for check in checks:
        style, label = _STATUS_STYLE[check.status]
        table.add_row(check.title, f"[{style}]{label}[/{style}]", check.detail)
    console.print(table)

    critical = [check for check in checks if check.critical]
    warnings = [check for check in checks if check.status == "warn"]
    if critical:
        console.print(f"[red]Критичные проблемы ({len(critical)}):[/red]")
        for check in critical:
            console.print(f"  • [red]{check.title}[/red]: {check.detail}")
        return True
    if warnings:
        console.print(
            f"[yellow]Предупреждений: {len(warnings)}[/yellow] — система запустится, "
            "но часть возможностей недоступна."
        )
    else:
        console.print("[green]Конфигурация в порядке.[/green]")
    if settings.queue.backend == "inline":
        console.print(
            "[dim]Подсказка: демо без Docker — SQLite + inline-очередь + LLM fake. "
            "Запуск: hirebot serve и hirebot worker --inline в другом терминале.[/dim]"
        )
    return False


@app.command(help="Проверить конфигурацию и доступность зависимостей.")
def doctor() -> None:
    """Показать отчёт о конфигурации; код возврата 1 при критичной проблеме."""
    settings = _load_settings()
    configure_logging(level=settings.log_level, json_output=False)
    checks = _run_async(_collect_checks(settings))
    if _render_checks(checks, settings):
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# db
# ---------------------------------------------------------------------------


async def _create_schema(settings: Settings) -> None:
    """Создать схему по моделям на отдельном движке.

    Движок создаётся здесь и закрывается сразу: команда одноразовая, держать
    пул соединений после её завершения незачем.
    """
    from hirebot.infra.db.session import create_engine, init_models

    engine = create_engine(settings)
    try:
        await init_models(engine)
    finally:
        await engine.dispose()


@db_app.command("init", help="Создать схему БД (для Alembic — alembic upgrade head).")
def db_init() -> None:
    """Создать таблицы по текущим моделям.

    Для продакшена используйте миграции: ``alembic upgrade head`` — так схема
    остаётся версионируемой и воспроизводимой.
    """
    settings = _load_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    try:
        _run_async(_create_schema(settings))
    except Exception as exc:
        err_console.print(f"[red]Не удалось создать схему:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc

    console.print(f"[green]Схема создана:[/green] {_redact_dsn(settings.database.dsn)}")
    console.print(
        "Дальше: [bold]alembic upgrade head[/bold] — применить миграции "
        "(обязательно для PostgreSQL в продакшене)."
    )


# ---------------------------------------------------------------------------
# poll
# ---------------------------------------------------------------------------


async def _poll_loop(settings: Settings, channel: Channel, *, once: bool, interval: int, limit: int) -> int:
    """Опрос канала: один проход или цикл с паузой."""
    from hirebot.worker import tasks

    container = await build_container(settings)
    ctx: dict[str, Any] = {"container": container, "job_id": f"cli-poll-{channel.value}"}
    fetched = 0
    try:
        while True:
            result = await tasks.poll_channel(ctx, channel.value, limit=limit)
            counters = result["counters"]
            fetched += int(counters.get("fetched", 0))
            rendered = ", ".join(f"{key}={value}" for key, value in sorted(counters.items()))
            console.print(f"[cyan]{channel.value}[/cyan]: {rendered or 'нет данных'}")
            if once:
                break
            await container.clock.sleep(interval)
    finally:
        await container.aclose()
    console.print(f"[green]Итого получено откликов:[/green] {fetched}")
    return fetched


@app.command(help="Запустить поллинг площадки (hh или avito).")
def poll(
    channel: Annotated[str, typer.Option("--channel", "-c", help="Площадка: hh | avito")],
    once: Annotated[bool, typer.Option("--once/--watch", help="Один проход или цикл до остановки")] = True,
    interval: Annotated[int, typer.Option("--interval", min=5, help="Пауза между проходами, секунд")] = 60,
    limit: Annotated[
        int, typer.Option("--limit", min=1, max=200, help="Сколько откликов забирать за проход")
    ] = 50,
) -> None:
    """Опросить площадку и поставить новые отклики в обработку."""
    settings = _load_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    try:
        channel_name = Channel(channel)
    except ValueError:
        allowed = ", ".join(item.value for item in Channel)
        err_console.print(f"[red]Неизвестный канал {channel!r}.[/red] Допустимо: {allowed}")
        raise typer.Exit(code=2) from None

    _run_async(_poll_loop(settings, channel_name, once=once, interval=interval, limit=limit))


# ---------------------------------------------------------------------------
# serve
# ---------------------------------------------------------------------------


@app.command(help="Запустить HTTP API (uvicorn).")
def serve(
    host: Annotated[str | None, typer.Option("--host", help="Адрес прослушивания")] = None,
    port: Annotated[int | None, typer.Option("--port", min=1, max=65535)] = None,
    reload: Annotated[bool, typer.Option("--reload", help="Автоперезапуск (только разработка)")] = False,
) -> None:
    """Поднять ASGI-приложение ``hirebot.api.app:app``."""
    settings = _load_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)
    try:
        import uvicorn
    except ImportError as exc:  # pragma: no cover - uvicorn в зависимостях проекта
        err_console.print(f"[red]uvicorn недоступен:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    bind_host = host or settings.api_host
    bind_port = port or settings.api_port
    console.print(f"[green]API:[/green] http://{bind_host}:{bind_port} (docs: /docs)")
    uvicorn.run(
        "hirebot.api.app:app",
        host=bind_host,
        port=bind_port,
        reload=reload,
        # Своя настройка логов: structlog уже сконфигурирован выше.
        log_config=None,
    )


# ---------------------------------------------------------------------------
# worker
# ---------------------------------------------------------------------------


async def _run_inline(settings: Settings, *, interval: float, once: bool) -> int:
    """Inline-воркер: без Redis и arq."""
    from hirebot.worker.inline import run_inline_once, run_inline_worker

    container = await build_container(settings)
    try:
        if once:
            return await run_inline_once(container, batch_size=INLINE_BATCH_SIZE)
        return await run_inline_worker(container, interval_s=interval)
    finally:
        await container.aclose()


@app.command(help="Запустить воркер (arq или inline без брокера).")
def worker(
    inline: Annotated[
        bool, typer.Option("--inline", help="Выполнять задачи в этом процессе, без Redis")
    ] = False,
    interval: Annotated[
        float, typer.Option("--interval", min=0.1, help="Пауза между проходами outbox, секунд")
    ] = 5.0,
    once: Annotated[
        bool, typer.Option("--once", help="Один проход диспетчера outbox и выход (для проверки)")
    ] = False,
    metrics_port: Annotated[
        int | None,
        typer.Option(
            "--metrics-port",
            min=1,
            max=65535,
            help="Порт /metrics этого воркера (0 или не задан — не поднимать)",
        ),
    ] = None,
) -> None:
    """Запустить обработку фоновых задач.

    ``--inline`` нужен для демо и отладки: контейнер собирается в этом процессе,
    брокер не требуется. Без ``--inline`` поднимается arq-воркер с cron-задачами
    поллинга и доставки outbox.

    ``--metrics-port`` поднимает в процессе воркера HTTP-эндпоинт метрик. Это
    важно: счётчики событий (скрининги, отправки, вызовы LLM) видны только там,
    где произошли, а основная работа идёт именно в воркере.
    """
    settings = _load_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    if metrics_port:
        from hirebot.observability.metrics import setup_metrics, start_worker_metrics_server

        setup_metrics(enabled=True)
        if start_worker_metrics_server(metrics_port):
            console.print(f"[green]Метрики воркера:[/green] http://127.0.0.1:{metrics_port}/metrics")

    if inline or once:
        delivered = _run_async(_run_inline(settings, interval=interval, once=once))
        console.print(f"[green]Inline-воркер остановлен.[/green] Доставлено записей: {delivered}")
        return

    if settings.queue.backend == "inline":
        console.print(
            "[yellow]Внимание:[/yellow] HIREBOT_QUEUE__BACKEND=inline — задачи выполняются "
            "в процессе API. Для демо используйте: [bold]hirebot worker --inline[/bold]"
        )
    try:
        from arq.worker import run_worker

        from hirebot.worker.settings import WorkerSettings
    except ImportError as exc:
        err_console.print(f"[red]arq недоступен:[/red] {exc}")
        raise typer.Exit(code=1) from exc

    console.print("[green]arq-воркер запущен[/green] (cron: поллинг 60с, outbox 10с)")
    # arq принимает настройки структурно (Protocol WorkerSettingsBase), а mypy
    # видит несовпадение ClassVar с членами протокола — срабатывание ложное.
    run_worker(WorkerSettings)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# demo
# ---------------------------------------------------------------------------


@app.command(help="Сквозной демо-прогон на тестовых адаптерах: без ключей, сети и Docker.")
def demo(
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Только итог, без пошагового вывода")] = False,
) -> None:
    """Показать полный сценарий: отклик -> скрининг -> диалог -> собеседование.

    Ничего не настраивает и не требует: все внешние системы подменены
    детерминированными реализациями, поэтому команду можно запускать сразу
    после установки зависимостей.
    """
    import io

    from rich.console import Console as RichConsole

    from hirebot.demo import render_summary_text, run_demo

    # Демо — это рассказ, а не отладка: логи воркеров перебивали бы его.
    # О штатной деградации (закрытое резюме) демо сообщает само, отдельной строкой.
    ensure_utf8_streams()
    configure_logging(level="ERROR", json_output=False)

    buffer = io.StringIO()
    # В тихом режиме вывод демо уходит в буфер и не попадает на экран.
    target = RichConsole(file=buffer, width=100, force_terminal=False) if quiet else console

    summary = _run_async(run_demo(console=target))

    if quiet:
        console.print(render_summary_text(summary))
        return

    if summary.errors:
        err_console.print(f"[yellow]Замечания при обработке: {len(summary.errors)}[/yellow]")


# ---------------------------------------------------------------------------
# evaluate
# ---------------------------------------------------------------------------


@app.command(help="Прогнать эталонную выборку и показать качество скрининга.")
def evaluate(
    dataset: Annotated[
        str,
        typer.Option("--dataset", "-d", help="Путь к JSON с эталонной выборкой"),
    ] = DEFAULT_DATASET_PATH,
    failures: Annotated[bool, typer.Option("--failures/--no-failures", help="Показать разбор ошибок")] = True,
) -> None:
    """Посчитать метрики скрининга на размеченной выборке.

    Замер идёт через настоящий ``ScreeningService``: пересчёт балла по весам и
    правила вердикта остаются нашими. Меняется только провайдер модели, поэтому
    тот же замер повторяется на реальной LLM (см. docs/EVALUATION.md).
    """
    from pathlib import Path

    from hirebot.evaluation import GoldenDatasetError, load_dataset, run_evaluation

    # Это отчёт, а не отладка: INFO-логи скрининга перебивали бы таблицу метрик.
    ensure_utf8_streams()
    configure_logging(level="WARNING", json_output=False)

    path = Path(dataset)
    if not path.is_file():
        err_console.print(
            f"[red]Выборка не найдена:[/red] {path}. "
            f"Укажите путь через --dataset или запустите из корня проекта."
        )
        raise typer.Exit(code=2)

    try:
        golden = load_dataset(path)
    except GoldenDatasetError as exc:
        err_console.print(f"[red]Некорректная выборка:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    report = _run_async(run_evaluation(golden))
    _render_evaluation(report, console, show_failures=failures)


def _render_evaluation(report: Any, target: Console, *, show_failures: bool) -> None:
    """Напечатать отчёт о качестве скрининга."""
    metrics = report.decision_metrics
    measured = Table(title=f"Качество скрининга: {report.model} (выборка {report.dataset_version})")
    measured.add_column("Метрика", style="bold")
    measured.add_column("Значение", justify="right")
    measured.add_row("Решение «приглашать»: precision", f"{metrics.precision:.3f}")
    measured.add_row("Решение «приглашать»: recall", f"{metrics.recall:.3f}")
    measured.add_row("F1", f"{metrics.f1:.3f}")
    measured.add_row("Accuracy", f"{metrics.accuracy:.3f}")
    measured.add_row("Specificity", f"{metrics.specificity:.3f}")
    measured.add_row(
        "Матрица ошибок",
        f"TP={metrics.true_positive} FP={metrics.false_positive} "
        f"TN={metrics.true_negative} FN={metrics.false_negative}",
    )
    measured.add_row("Точность вердикта", f"{report.verdict_accuracy:.3f}")
    measured.add_row("Точность по обязательным критериям", f"{report.must_have_accuracy:.3f}")
    measured.add_row("Всего случаев", str(len(report.outcomes)))
    target.print(measured)

    if metrics.false_positive or metrics.false_negative:
        target.print(
            "  [yellow]Ложное приглашение[/yellow] тратит время рекрутера, "
            "[yellow]ложный отказ[/yellow] теряет кандидата — обе ошибки важны."
        )

    if not show_failures or not report.failures:
        return

    problems = Table(title=f"Расхождения с разметкой: {len(report.failures)}")
    problems.add_column("Случай", style="bold", overflow="fold")
    problems.add_column("Ожидалось")
    problems.add_column("Получено")
    problems.add_column("Балл", justify="right")
    for outcome in report.failures:
        expected = "приглашать" if outcome.expected_should_invite else "не приглашать"
        actual = "приглашать" if outcome.actual_should_invite else "не приглашать"
        if outcome.expected_should_invite != outcome.actual_should_invite:
            expected = f"[red]{expected}[/red]"
            actual = f"[red]{actual}[/red]"
        problems.add_row(outcome.case_id, expected, actual, f"{outcome.score:.1f}")
    target.print(problems)
    target.print(
        "  [dim]Разбор причин — в docs/EVALUATION.md. "
        "Выборка расширяется: чем больше случаев, тем меньше доверия к случайности.[/dim]"
    )


# ---------------------------------------------------------------------------
# seed
# ---------------------------------------------------------------------------


@app.command(help="Наполнить локальную базу демонстрационными диалогами для /ui.")
def seed(
    reset: Annotated[
        bool, typer.Option("--reset", help="Создать демо-данные заново, даже если они есть")
    ] = False,
) -> None:
    """Создать демо-диалоги и прогнать по ним настоящий скрининг.

    Нужно, чтобы открыть интерфейс рекрутера с данными: реальные площадки без
    токенов ничего не отдают. Скрининг считается боевым ``ScreeningService``.
    """

    settings = _load_settings()
    configure_logging(level=settings.log_level, json_output=settings.log_json)

    try:
        created = _run_async(_seed_async(settings, reset=reset))
    except Exception as exc:
        err_console.print(f"[red]Не удалось создать демо-данные:[/red] {type(exc).__name__}: {exc}")
        raise typer.Exit(code=1) from exc

    if created == 0:
        console.print(
            "[yellow]Демо-данные уже есть.[/yellow] Запустите с [bold]--reset[/bold], чтобы создать новые."
        )
        return

    console.print(f"[green]Создано диалогов:[/green] {created}")
    console.print("Откройте интерфейс: [bold]http://127.0.0.1:8000/ui[/bold] (после hirebot serve)")


async def _seed_async(settings: Settings, *, reset: bool) -> int:
    from hirebot.container import build_container
    from hirebot.infra.db.session import ensure_local_schema
    from hirebot.seed import seed_demo_data

    await ensure_local_schema(settings)
    container = await build_container(settings)
    try:
        return await seed_demo_data(container, reset=reset)
    finally:
        await container.aclose()


# ---------------------------------------------------------------------------
# approvals
# ---------------------------------------------------------------------------

approvals_app = typer.Typer(
    name="approvals",
    help="Очередь подтверждения: черновики сообщений кандидатам.",
    no_args_is_help=True,
)
app.add_typer(approvals_app, name="approvals")


@approvals_app.command("list", help="Показать черновики, ожидающие решения.")
def approvals_list(
    limit: Annotated[int, typer.Option("--limit", min=1, max=200)] = 50,
) -> None:
    """Список черновиков: кто, куда и что бот предлагает отправить."""
    from hirebot.services.approval import ApprovalService

    settings = _load_settings()
    configure_logging(level="WARNING", json_output=False)

    async def _load() -> Any:
        container = await build_container(settings)
        try:
            return await ApprovalService(container).list_pending(limit=limit)
        finally:
            await container.aclose()

    pending = _run_async(_load())
    if not pending:
        console.print("[yellow]Очередь пуста.[/yellow]")
        console.print("Черновики появляются при HIREBOT_POLICY__REQUIRE_HUMAN_APPROVAL=true.")
        return

    table = Table(title=f"На подтверждении: {len(pending)}")
    table.add_column("ID черновика", style="dim", no_wrap=True)
    table.add_column("Кандидат", style="bold")
    table.add_column("Вакансия")
    table.add_column("Тип")
    table.add_column("Текст", overflow="fold")
    for item in pending:
        table.add_row(
            str(item.draft_id),
            item.candidate_name,
            item.vacancy_title,
            item.kind,
            item.body[:160],
        )
    console.print(table)
    console.print("Одобрить: [bold]hirebot approvals approve <id>[/bold]")


@approvals_app.command("approve", help="Одобрить черновик и отправить сообщение.")
def approvals_approve(
    draft_id: Annotated[str, typer.Argument(help="Идентификатор черновика")],
    text: Annotated[str | None, typer.Option("--text", "-t", help="Заменить текст перед отправкой")] = None,
    reviewer: Annotated[str, typer.Option("--reviewer", help="Кто одобрил")] = "cli",
) -> None:
    """Отправить сообщение кандидату после подтверждения человеком."""
    from hirebot.services.approval import ApprovalService

    settings = _load_settings()
    configure_logging(level="WARNING", json_output=False)

    async def _approve() -> Any:
        container = await build_container(settings)
        try:
            return await ApprovalService(container).approve(
                _parse_uuid_or_exit(draft_id, "draft_id"), reviewer=reviewer, text=text
            )
        finally:
            await container.aclose()

    message = _run_async(_approve())
    console.print(f"[green]Отправлено.[/green] Сообщение {message.id}")


@approvals_app.command("reject", help="Отклонить черновик: кандидату ничего не уйдёт.")
def approvals_reject(
    draft_id: Annotated[str, typer.Argument(help="Идентификатор черновика")],
    reason: Annotated[str | None, typer.Option("--reason", "-r", help="Причина")] = None,
    reviewer: Annotated[str, typer.Option("--reviewer", help="Кто отклонил")] = "cli",
) -> None:
    """Отклонить черновик. Диалог остаётся человеку, кандидат не получает отказ."""
    from hirebot.services.approval import ApprovalService

    settings = _load_settings()
    configure_logging(level="WARNING", json_output=False)

    async def _reject() -> None:
        container = await build_container(settings)
        try:
            await ApprovalService(container).reject(
                _parse_uuid_or_exit(draft_id, "draft_id"), reviewer=reviewer, reason=reason
            )
        finally:
            await container.aclose()

    _run_async(_reject())
    console.print("[yellow]Черновик отклонён.[/yellow] Кандидату ничего не отправлено.")


def _parse_uuid_or_exit(value: str, field: str) -> Any:
    """Разобрать UUID или завершить команду понятной ошибкой."""
    import uuid as uuid_module

    try:
        return uuid_module.UUID(value)
    except ValueError:
        err_console.print(f"[red]Некорректный {field}:[/red] {value!r}")
        raise typer.Exit(code=2) from None


# ---------------------------------------------------------------------------
# privacy (152-ФЗ)
# ---------------------------------------------------------------------------

privacy_app = typer.Typer(
    name="privacy",
    help="Сроки хранения, экспорт и удаление персональных данных (152-ФЗ).",
    no_args_is_help=True,
)
app.add_typer(privacy_app, name="privacy")


@privacy_app.command("purge", help="Удалить данные, срок хранения которых истёк.")
def privacy_purge(
    dry_run: Annotated[
        bool, typer.Option("--dry-run", help="Только показать сроки, ничего не удалять")
    ] = False,
) -> None:
    """Применить сроки хранения из ``HIREBOT_PRIVACY__*``.

    Задачу стоит запускать по расписанию: хранение ПДн «на всякий случай»
    нарушает 152-ФЗ, а не просто занимает место.
    """
    from hirebot.services.privacy import PrivacyService

    settings = _load_settings()
    configure_logging(level="WARNING", json_output=False)
    privacy = settings.privacy

    if dry_run:
        table = Table(title="Сроки хранения (dry-run)")
        table.add_column("Что", style="bold")
        table.add_column("Срок, дней", justify="right")
        table.add_row("Тексты резюме", str(privacy.resume_retention_days))
        table.add_row("История переписки", str(privacy.message_retention_days))
        table.add_row("Результаты скрининга", str(privacy.screening_retention_days))
        table.add_row("Черновики", str(privacy.draft_retention_days))
        table.add_row("Журнал действий", str(privacy.audit_retention_days))
        console.print(table)
        console.print("[dim]Ничего не удалено: запустите без --dry-run.[/dim]")
        return

    async def _purge() -> Any:
        container = await build_container(settings)
        try:
            return await PrivacyService(container).purge_expired()
        finally:
            await container.aclose()

    report = _run_async(_purge())
    counts = report.stats.as_dict()
    if report.total == 0:
        console.print("[green]Нечего удалять: сроки хранения не истекли.[/green]")
        return
    console.print(f"[green]Удалено записей:[/green] {report.total}")
    for name, value in counts.items():
        if value:
            console.print(f"  {name}: {value}")


@privacy_app.command("export", help="Выгрузить все данные кандидата в JSON.")
def privacy_export(
    candidate_id: Annotated[str, typer.Argument(help="Идентификатор кандидата")],
    out: Annotated[
        str | None, typer.Option("--out", "-o", help="Файл для выгрузки (по умолчанию stdout)")
    ] = None,
) -> None:
    """Право субъекта на доступ к своим данным: один JSON со всем, что хранится."""
    import json as json_module
    from pathlib import Path

    from hirebot.services.privacy import PrivacyService

    settings = _load_settings()
    configure_logging(level="WARNING", json_output=False)

    async def _export() -> Any:
        container = await build_container(settings)
        try:
            return await PrivacyService(container).export_candidate(
                _parse_uuid_or_exit(candidate_id, "candidate_id")
            )
        finally:
            await container.aclose()

    payload = _run_async(_export())
    if payload is None:
        err_console.print(f"[red]Кандидат не найден:[/red] {candidate_id}")
        raise typer.Exit(code=1)

    text = json_module.dumps(payload, ensure_ascii=False, indent=2)
    if out:
        Path(out).write_text(text, encoding="utf-8")
        console.print(f"[green]Выгружено в[/green] {out}")
    else:
        console.print_json(text)


@privacy_app.command("erase", help="Удалить персональные данные кандидата.")
def privacy_erase(
    candidate_id: Annotated[str, typer.Argument(help="Идентификатор кандидата")],
    yes: Annotated[bool, typer.Option("--yes", "-y", help="Подтвердить удаление")] = False,
) -> None:
    """Право на удаление: тексты и контакты удаляются, воронка обезличивается.

    Действие необратимо, поэтому без ``--yes`` ничего не происходит.
    """
    from hirebot.services.privacy import PrivacyService

    if not yes:
        err_console.print(
            "[yellow]Удаление необратимо.[/yellow] Повторите с [bold]--yes[/bold], если решение принято."
        )
        raise typer.Exit(code=2)

    settings = _load_settings()
    configure_logging(level="WARNING", json_output=False)

    async def _erase() -> bool:
        container = await build_container(settings)
        try:
            return await PrivacyService(container).erase_candidate(
                _parse_uuid_or_exit(candidate_id, "candidate_id")
            )
        finally:
            await container.aclose()

    erased = _run_async(_erase())
    if erased:
        console.print("[green]Персональные данные удалены.[/green] Воронка обезличена.")
    else:
        err_console.print(f"[red]Кандидат не найден:[/red] {candidate_id}")
        raise typer.Exit(code=1)


# ---------------------------------------------------------------------------
# walkthrough
# ---------------------------------------------------------------------------


@app.command(help="Пошаговый разбор обработки отклика: промежуточные данные на каждом шаге.")
def walkthrough(
    quiet: Annotated[bool, typer.Option("--quiet", "-q", help="Только итоговая сводка")] = False,
) -> None:
    """Показать, что происходит между откликом и встречей в календаре.

    В отличие от ``demo`` (витрина), здесь печатаются промежуточные данные:
    что пришло от площадки, как посчитан балл, какое решение принято и что
    осталось в системе. Сети и ключей не требуется.
    """
    import io

    from rich.console import Console as RichConsole

    from hirebot.walkthrough import format_summary, run_walkthrough

    ensure_utf8_streams()
    configure_logging(level="ERROR", json_output=False)

    buffer = io.StringIO()
    target = RichConsole(file=buffer, width=110, force_terminal=False) if quiet else console

    summary = _run_async(run_walkthrough(console=target))

    if quiet:
        console.print(format_summary(summary))


# ---------------------------------------------------------------------------
# criteria
# ---------------------------------------------------------------------------


@app.command(help="Справочник критериев подбора: из чего выбирать критерии вакансии.")
def criteria(
    category: Annotated[
        str | None, typer.Option("--category", "-c", help="Показать только эту категорию")
    ] = None,
    markdown: Annotated[
        bool, typer.Option("--markdown", help="Вывести справочник в markdown (для документа)")
    ] = False,
    catalog_path: Annotated[
        str, typer.Option("--catalog", help="Путь к каталогу критериев")
    ] = DEFAULT_CATALOG_PATH,
) -> None:
    """Показать каталог критериев, из которого выбираются критерии вакансии.

    Критерии не выдумываются системой: это справочник, из которого рекрутер
    вместе с нанимающим менеджером выбирает нужное. Выбранные ключи передаются
    в вакансию как ``criteria``.
    """
    from hirebot.criteria import CatalogError, load_catalog, render_markdown

    ensure_utf8_streams()
    try:
        catalog = load_catalog(catalog_path)
    except CatalogError as exc:
        err_console.print(f"[red]Каталог критериев некорректен:[/red] {exc}")
        raise typer.Exit(code=2) from exc

    if markdown:
        console.print(render_markdown(catalog), markup=False, highlight=False)
        return

    selected = catalog.by_category()
    shown = 0
    for key, group in catalog.categories.items():
        if category is not None and key != category:
            continue
        items = selected.get(key, [])
        if not items:
            continue
        shown += len(items)
        table = Table(title=f"{group.title} ({len(items)})", show_lines=False)
        table.add_column("Ключ", style="cyan", no_wrap=True)
        table.add_column("Критерий", style="bold", overflow="fold")
        table.add_column("Проверяемость", no_wrap=True)
        table.add_column("Вес", justify="right", no_wrap=True)
        table.add_column("Обязат.", justify="center", no_wrap=True)
        for item in items:
            title = item.title + (" ⚖" if item.needs_lawyer else "")
            table.add_row(
                item.key,
                title,
                item.measurable_label,
                str(item.weight_hint),
                "да" if item.must_have_hint else "—",
            )
        console.print(table)
        if group.description:
            console.print(f"  [dim]{group.description}[/dim]")

    console.print(
        f"\nВсего показано: [bold]{shown}[/bold] из {len(catalog.criteria)}. ⚖ — требует решения юриста."
    )
    console.print(
        "[dim]Вес и обязательность здесь — подсказка. Итоговые значения утверждает "
        "рекрутер под вакансию.[/dim]"
    )
    if catalog.forbidden:
        console.print(
            f"[red]Запрещённых признаков в каталоге нет[/red] и быть не может: "
            f"{len(catalog.forbidden)} групп (пол, возраст, национальность и др.) "
            "заблокированы правилами."
        )


# ---------------------------------------------------------------------------
# Точка входа
# ---------------------------------------------------------------------------


def _version_callback(value: bool) -> None:
    if value:
        console.print(f"hirebot {__version__}")
        raise typer.Exit


@app.callback()
def main_callback(
    _version: Annotated[
        bool,
        typer.Option("--version", callback=_version_callback, is_eager=True, help="Показать версию"),
    ] = False,
) -> None:
    """hirebot: AI-рекрутер поверх hh.ru, Avito и календаря Talantix."""


def main() -> None:
    """Точка входа консольной команды ``hirebot``."""
    app()


if __name__ == "__main__":
    main()
