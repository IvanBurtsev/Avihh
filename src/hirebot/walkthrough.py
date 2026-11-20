"""Пошаговый разбор обработки отклика: что происходит внутри.

Отличие от ``hirebot demo``:

* ``demo`` — витрина: показывает результат конечному пользователю;
* ``walkthrough`` — разбор: печатает **промежуточные данные** на каждом шаге,
  чтобы было видно, что именно пришло от площадки, как это преобразовалось,
  какой балл и почему получился, и какое решение принято.

Модуль используется и в CLI (``hirebot walkthrough``), и в документации: вывод
воспроизводим, потому что часы и адаптеры детерминированы.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from hirebot.config import PolicySettings, Settings
from hirebot.container import Container, build_fake_container
from hirebot.domain.entities import Conversation, ScreeningResult
from hirebot.domain.enums import Channel, ConversationState, MessageDirection
from hirebot.ports.dto import InboundMessage
from hirebot.services import ConversationService, IngestService, SchedulingService
from hirebot.testing.fakes import FakeChannelAdapter

#: Отклик, который «пришёл с площадки» в этом разборе.
CANDIDATE_NAME = "Алексей Петров"

INTERESTED_REPLY = "Да, интересно. Удобно во вторник в 11:00."


@dataclass(slots=True)
class Step:
    """Один шаг разбора: заголовок и что произошло."""

    number: int
    title: str
    note: str = ""


def _console(console: Console | None) -> Console:
    return console or Console()


def _heading(out: Console, step: Step) -> None:
    out.print()
    out.rule(f"[bold cyan]Шаг {step.number}.[/bold cyan] {step.title}", align="left")
    if step.note:
        out.print(f"  [dim]{escape(step.note)}[/dim]")


def _kv(out: Console, pairs: list[tuple[str, str]], *, title: str = "") -> None:
    """Печать пар «ключ → значение» без лишнего оформления."""
    table = Table(show_header=False, box=None, padding=(0, 1))
    table.add_column("k", style="dim", no_wrap=True)
    table.add_column("v")
    for key, value in pairs:
        table.add_row(key, escape(value))
    if title:
        out.print(f"  [bold]{escape(title)}[/bold]")
    out.print(table)


async def run_walkthrough(*, console: Console | None = None) -> dict[str, Any]:
    """Прогнать полный цикл и напечатать разбор.

    Возвращает сводку — она используется тестами, чтобы разбор не разошёлся
    с фактическим поведением системы.
    """
    out = _console(console)
    # Разбор идёт в режиме обязательного подтверждения: именно так бот должен
    # работать на реальных кандидатах, и именно этот режим здесь показывается.
    settings = Settings(
        env="test",
        log_level="ERROR",
        log_json=False,
        _env_file=None,
        policy=PolicySettings(require_human_approval=True),
    )
    container = build_fake_container(settings)
    channel = container.channels[Channel.HH]
    assert isinstance(channel, FakeChannelAdapter)

    summary: dict[str, Any] = {}

    out.print(
        Panel.fit(
            "[bold]Разбор обработки отклика[/bold]\n"
            "[dim]Что происходит между «кандидат нажал откликнуться» и «встреча в календаре».[/dim]",
            border_style="cyan",
        )
    )

    # ------------------------------------------------------------------ 1 --
    _heading(
        out,
        Step(
            1,
            "Площадка отдаёт отклик",
            "hh.ru присылает отклик: кто, на какую вакансию, с каким резюме.",
        ),
    )
    _kv(
        out,
        [
            ("канал", "hh"),
            ("отклик (внешний id)", "hh-chat-1001"),
            ("кандидат", CANDIDATE_NAME),
            ("кандидат (внешний id)", "hh-candidate-1001"),
            ("вакансия", "hh-vacancy-python-001 · Python-разработчик"),
            ("резюме (внешний id)", "hh-resume-1001"),
            ("сопроводительное", "«Заинтересовала вакансия, готов обсудить детали»"),
        ],
        title="Входящие данные:",
    )

    # ------------------------------------------------------------------ 2 --
    _heading(
        out,
        Step(
            2,
            "Поллинг: забираем только новое",
            "Воркер опрашивает площадку по расписанию. Дедупликация — по (канал, внешний id).",
        ),
    )
    ingest = IngestService(container)
    poll = await ingest.poll(Channel.HH)
    _kv(
        out,
        [
            ("найдено откликов", str(poll.found)),
            ("новых диалогов", str(poll.created)),
            ("уже обработанных", str(poll.duplicates)),
            ("ошибок", str(len(poll.errors))),
        ],
        title="Результат поллинга:",
    )

    again = await ingest.poll(Channel.HH)
    out.print(
        f"  Повторный поллинг: новых [bold]{again.created}[/bold] — "
        "[dim]отклик уже обработан, второй раз в работу не пойдёт[/dim]"
    )

    conversation = await _find_conversation(container, CANDIDATE_NAME)
    summary["conversation_id"] = str(conversation.id)
    out.print(
        f"  Создан диалог [dim]{conversation.id}[/dim], состояние: [bold]{conversation.state.value}[/bold]"
    )

    # ------------------------------------------------------------------ 3 --
    _heading(
        out,
        Step(
            3,
            "Читаем резюме",
            "Резюме запрашивается отдельным вызовом: у hh.ru он платный и квотируемый.",
        ),
    )
    screening = await _screen(container, conversation)
    summary["score"] = screening.total_score
    summary["verdict"] = screening.verdict.value
    if screening.model:
        out.print(f"  Модель: [bold]{screening.model}[/bold], версия промпта: {screening.prompt_version}")
    out.print(f"  Заключение модели: [dim]{escape(screening.summary[:120]) or '—'}[/dim]")
    out.print("  [dim]Текст резюме уходит в модель, но итоговый балл считается не ей.[/dim]")

    # ------------------------------------------------------------------ 4 --
    _heading(
        out,
        Step(
            4,
            "Скоринг по критериям вакансии",
            "Модель даёт оценку и цитату по каждому критерию. Итоговый балл считаем мы.",
        ),
    )
    _print_criteria(out, screening)
    _print_scoring_math(out, screening)

    # ------------------------------------------------------------------ 5 --
    _heading(
        out,
        Step(
            5,
            "Решение по правилам",
            "Вердикт — не ответ модели, а наша логика поверх её оценок.",
        ),
    )
    _print_verdict(out, container, screening)
    summary["verdict_policy"] = screening.verdict.value

    # ------------------------------------------------------------------ 6 --
    _heading(
        out,
        Step(
            6,
            "Шаг воронки: что делает бот",
            "Решение зависит от вердикта, состояния диалога и настроек политики.",
        ),
    )
    service = ConversationService(container)
    outcome = await service.process(conversation.id)
    summary["action"] = outcome.action_taken
    _kv(
        out,
        [
            ("действие", outcome.action_taken),
            ("новое состояние", outcome.state.value),
            ("пояснение", outcome.detail or "—"),
            ("использована LLM", "да" if outcome.used_llm else "нет"),
        ],
        title="Результат шага:",
    )

    # ------------------------------------------------------------------ 7 --
    _heading(
        out,
        Step(
            7,
            "Черновик на подтверждение",
            "Бот написал текст, но кандидату ничего не ушло: включён режим апрува.",
        ),
    )
    from hirebot.services.approval import ApprovalService

    approval = ApprovalService(container)
    pending = await approval.list_pending()
    draft = next((item for item in pending if item.conversation_id == conversation.id), None)
    sent_before = len(channel.texts_for(conversation.external_id))
    if draft is not None:
        _kv(
            out,
            [
                ("кандидат", draft.candidate_name),
                ("тип", draft.kind),
                ("причина", draft.reason or "—"),
                ("модель", draft.model or "—"),
            ],
            title="Черновик:",
        )
        out.print(Panel(escape(draft.body), title="Текст для кандидата", border_style="yellow"))
    out.print(
        f"  Сообщений в чате площадки: [bold]{sent_before}[/bold] — [dim]кандидат ещё ничего не получил[/dim]"
    )

    # ------------------------------------------------------------------ 8 --
    _heading(
        out,
        Step(
            8,
            "Рекрутер одобряет",
            "Человек читает, при необходимости правит текст и отправляет.",
        ),
    )
    if draft is not None:
        await approval.approve(draft.draft_id, reviewer="recruiter")
        sent_after = len(channel.texts_for(conversation.external_id))
        out.print(f"  Одобрено → сообщений в чате: [bold green]{sent_after}[/bold green]")
        summary["sent_after_approval"] = sent_after

    # ------------------------------------------------------------------ 9 --
    _heading(
        out,
        Step(
            9,
            "Кандидат отвечает",
            "Ответ приходит в тот же чат. Бот читает новые сообщения и определяет интент.",
        ),
    )
    channel.add_inbound_message(
        conversation.external_id,
        InboundMessage(
            external_id="hh-msg-in-1",
            direction=MessageDirection.INBOUND,
            text=INTERESTED_REPLY,
            created_at=container.clock.now(),
            author_name=CANDIDATE_NAME,
        ),
    )
    out.print(f"  [bold]{escape(CANDIDATE_NAME)}[/bold]: «{INTERESTED_REPLY}»")

    outcome = await service.process(conversation.id)
    summary["reply_action"] = outcome.action_taken
    _kv(
        out,
        [
            ("распознанный интент", "заинтересован, выбрал время"),
            ("действие", outcome.action_taken),
            ("состояние", outcome.state.value),
        ],
        title="Разбор ответа:",
    )

    # ----------------------------------------------------------------- 10 --
    _heading(
        out,
        Step(
            10,
            "Слоты из календаря",
            "Занятость читается из Talantix, сетка строится у нас: рабочие часы и буферы.",
        ),
    )
    scheduling = SchedulingService(container)
    slots = await scheduling.propose_slots(conversation_id=conversation.id, limit=3)
    _print_slots(out, slots)
    summary["slots"] = len(slots)

    # ----------------------------------------------------------------- 11 --
    _heading(
        out,
        Step(
            11,
            "Бронь: проверка, создание, верификация",
            "Действие рекрутера из интерфейса: слот адресуется временем, занятость "
            "перечитывается, встреча подтверждается.",
        ),
    )
    if slots:
        chosen = slots[0]
        booking = await scheduling.book(
            conversation_id=conversation.id,
            slot_start=chosen.start,
            requested_by="recruiter",
        )
        summary["booked"] = True
        _kv(
            out,
            [
                ("начало", booking.start.isoformat()),
                ("менеджер", booking.manager_id),
                ("часовой пояс", booking.timezone),
                ("встреча в календаре", booking.external_meeting_id or "—"),
                ("статус", booking.status.value),
            ],
            title="Бронь:",
        )
        out.print(
            "  [dim]Встреча перечитана из календаря: бронь считается состоявшейся только "
            "после этого подтверждения.[/dim]"
        )
    else:
        summary["booked"] = False
        out.print("  [yellow]Свободных слотов нет — диалог уходит рекрутеру.[/yellow]")

    # ----------------------------------------------------------------- 12 --
    _heading(
        out,
        Step(
            12,
            "Что осталось в системе",
            "Состояние и история: по ним работают интерфейс, аудит и поддержка.",
        ),
    )
    async with container.uow_factory() as uow:
        final = await uow.conversations.get(conversation.id)
        messages = list(await uow.messages.list_for_conversation(conversation.id, limit=50))

    _kv(
        out,
        [
            ("состояние диалога", final.state.value if final else "—"),
            ("сообщений в истории", str(len(messages))),
            ("автосообщений бота", str(final.auto_messages_sent) if final else "0"),
        ],
        title="Итог:",
    )
    for message in messages:
        who = "Бот" if message.direction is MessageDirection.OUTBOUND else "Кандидат"
        out.print(f"  [dim]{who}:[/dim] {escape(message.body[:96])}…")

    await container.aclose()
    return summary


# --------------------------------------------------------------------------- #
# Печать промежуточных данных
# --------------------------------------------------------------------------- #


async def _find_conversation(container: Container, name: str) -> Conversation:
    async with container.uow_factory() as uow:
        conversations = await uow.conversations.list_by_state(list(ConversationState), limit=50)
        for conversation in conversations:
            candidate = await uow.candidates.get(conversation.candidate_id)
            if candidate is not None and candidate.full_name == name:
                return conversation
    msg = f"В демо-данных нет диалога кандидата {name!r}"
    raise RuntimeError(msg)


async def _screen(container: Container, conversation: Conversation) -> ScreeningResult:
    from hirebot.services.screening import ScreeningService

    return await ScreeningService(container).screen(conversation_id=conversation.id)


def _print_criteria(out: Console, screening: ScreeningResult) -> None:
    table = Table(show_lines=False)
    table.add_column("Критерий", style="bold")
    table.add_column("Обязательный", justify="center")
    table.add_column("Вес", justify="right")
    table.add_column("Оценка", justify="right")
    table.add_column("Цитата из резюме", overflow="fold")
    for criterion in screening.criteria:
        table.add_row(
            escape(criterion.title),
            "да" if criterion.must_have else "—",
            f"{criterion.weight:g}",
            f"[red]{criterion.score:.0f}[/red]" if criterion.is_gap else f"{criterion.score:.0f}",
            escape(criterion.evidence[:90]) if criterion.evidence else "[dim]нет цитаты → не зачтено[/dim]",
        )
    out.print(table)


def _print_scoring_math(out: Console, screening: ScreeningResult) -> None:
    weighted = sum(item.score * item.weight for item in screening.criteria)
    total_weight = sum(item.weight for item in screening.criteria) or 1.0
    out.print(
        "  [bold]Формула:[/bold] Σ(оценка × вес) / Σ(вес) = "
        f"({weighted:.1f}) / ({total_weight:g}) = [bold]{weighted / total_weight:.1f}[/bold]"
    )
    if screening.must_have_failures:
        gaps = ", ".join(item.title for item in screening.must_have_failures)
        out.print(f"  [red]Провал обязательного критерия:[/red] {escape(gaps)} → потолок балла")
    out.print(f"  Итоговый балл: [bold]{screening.total_score:.1f}[/bold]")


def _print_verdict(out: Console, container: Container, screening: ScreeningResult) -> None:
    threshold = container.settings.policy.min_score_to_invite
    _kv(
        out,
        [
            ("балл", f"{screening.total_score:.1f}"),
            ("порог приглашения", f"{threshold:g}"),
            ("вердикт", screening.verdict.value),
            ("провалов обязательных", str(len(screening.must_have_failures))),
        ],
        title="Правила вердикта:",
    )


def _print_slots(out: Console, slots: list[Any]) -> None:
    if not slots:
        out.print("  [yellow]Слотов нет.[/yellow]")
        return
    table = Table(show_header=True)
    table.add_column("№", justify="right", style="cyan")
    table.add_column("Начало (UTC)")
    table.add_column("Менеджер", style="dim")
    for index, slot in enumerate(slots, start=1):
        table.add_row(str(index), slot.start.isoformat(), slot.manager_id)
    out.print(table)
    out.print("  [dim]Время в UTC; в интерфейсе показывается в часовом поясе участника.[/dim]")


def format_summary(summary: dict[str, Any], *, at: datetime | None = None) -> str:
    """Короткая текстовая сводка разбора (для тестов)."""
    moment = at or datetime.now(tz=UTC)
    stamp = moment.strftime("%H:%M")
    return (
        f"[{stamp}] балл={summary.get('score')}, вердикт={summary.get('verdict')}, "
        f"действие={summary.get('action')}, отправлено={summary.get('sent_after_approval')}, "
        f"бронь={summary.get('booked')}"
    )


__all__ = ["CANDIDATE_NAME", "INTERESTED_REPLY", "Step", "format_summary", "run_walkthrough"]
