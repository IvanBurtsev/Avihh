"""Демонстрационный прогон сквозного сценария на fake-адаптерах.

Зачем отдельный модуль: это единственный способ показать продукт целиком —
от отклика до забронированного собеседования — **без единого внешнего ключа,
без сети и без Docker**. Прогон идёт по тому же коду, что и продакшен: те же
сервисы, тот же конечный автомат, тот же outbox. Подменены только адаптеры
площадок, календаря и модели.

Печатать напрямую нельзя (правило ``T20``): вывод идёт через ``rich.Console``,
который в тестах подменяется на буфер.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from uuid import UUID
from zoneinfo import ZoneInfo

from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from hirebot.container import Container, build_fake_container
from hirebot.domain.entities import Booking, Conversation, ScreeningResult
from hirebot.domain.enums import Channel, ConversationState, MessageDirection
from hirebot.ports.dto import InboundMessage
from hirebot.services import ConversationService, IngestService, SchedulingService
from hirebot.testing.fakes import FakeChannelAdapter

#: Реплики кандидата в демо: сначала интерес, затем выбор слота.
INTERESTED_REPLY = "Да, интересно, готов обсудить детали."
SLOT_CHOICE_REPLY = "Подходит вариант 1, давайте в это время."

WEEKDAYS = ("пн", "вт", "ср", "чт", "пт", "сб", "вс")


@dataclass(slots=True)
class CandidateCard:
    """Карточка кандидата для итогового отчёта."""

    conversation_id: UUID
    name: str
    state: ConversationState
    score: float | None = None
    verdict: str = "—"
    action: str = "—"
    booking: Booking | None = None


@dataclass(slots=True)
class DemoSummary:
    """Результат прогона — структура, пригодная для проверки автотестом."""

    found: int = 0
    created: int = 0
    duplicates: int = 0
    repeat_created: int = 0
    errors: list[str] = field(default_factory=list)
    candidates: list[CandidateCard] = field(default_factory=list)

    @property
    def booked(self) -> list[CandidateCard]:
        return [card for card in self.candidates if card.booking is not None]

    @property
    def best(self) -> CandidateCard | None:
        scored = [card for card in self.candidates if card.score is not None]
        return max(scored, key=lambda card: card.score or 0.0) if scored else None


async def run_demo(*, console: Console | None = None) -> DemoSummary:
    """Прогнать полный сценарий и напечатать его ход."""
    out = console or Console()
    container = build_fake_container()
    summary = DemoSummary()

    _print_intro(out, container)

    # --- Шаг 1. Поллинг площадки -------------------------------------------
    _heading(out, "1", "Сбор свежих откликов с hh.ru")
    ingest = IngestService(container)
    poll = await ingest.poll(Channel.HH)
    summary.found = poll.found
    summary.created = poll.created
    summary.duplicates = poll.duplicates
    summary.errors = list(poll.errors)
    out.print(
        f"  Найдено откликов: [bold]{poll.found}[/bold] | "
        f"новых диалогов: [bold green]{poll.created}[/bold green] | "
        f"пропущено как обработанные: {poll.duplicates}"
    )

    # Повторный поллинг демонстрирует идемпотентность приёма откликов.
    again = await ingest.poll(Channel.HH)
    summary.repeat_created = again.created
    out.print(
        f"  Повторный поллинг создал новых диалогов: [bold]{again.created}[/bold] "
        "— повторная обработка одного отклика исключена"
    )

    # --- Шаг 2. Скрининг и первый контакт ----------------------------------
    _heading(out, "2", "Анализ резюме и первый контакт")
    conversations = await _load_conversations(container)
    service = ConversationService(container)

    for conversation in conversations:
        outcome = await service.process(conversation.id)
        card = await _build_card(container, conversation, outcome.action_taken)
        summary.candidates.append(card)
        _print_screening(out, card)

    # --- Шаг 3. Ответ кандидата --------------------------------------------
    _heading(out, "3", "Кандидат отвечает — бот предлагает время")
    best = summary.best
    if best is not None:
        await _simulate_reply(container, best, INTERESTED_REPLY)
        outcome = await service.process(best.conversation_id)
        best.action = outcome.action_taken
        out.print(f"  [bold]{escape(best.name)}[/bold]: «{INTERESTED_REPLY}»")

        slots = await SchedulingService(container).propose_slots(
            conversation_id=best.conversation_id, limit=3
        )
        if slots:
            table = Table(title="Свободные слоты (время кандидата)")
            table.add_column("№", justify="right", style="cyan", no_wrap=True)
            table.add_column("Когда", style="bold")
            table.add_column("Менеджер", style="dim")
            for index, slot in enumerate(slots, start=1):
                table.add_row(str(index), _format_slot(slot.start, slot.timezone), slot.manager_id)
            out.print(table)
            out.print(
                "  [dim]Формулировка намеренно осторожная: система видит только занятость "
                "в Talantix и не знает о личных встречах вне CRM.[/dim]"
            )
        else:
            out.print("  [yellow]Свободных слотов нет — диалог уйдёт рекрутеру.[/yellow]")

        # --- Шаг 4. Выбор слота и бронь -----------------------------------
        _heading(out, "4", "Выбор времени и запись в календарь Talantix")
        out.print(f"  [bold]{escape(best.name)}[/bold]: «{SLOT_CHOICE_REPLY}»")
        await _simulate_reply(container, best, SLOT_CHOICE_REPLY)
        outcome = await service.process(best.conversation_id)
        best.action = outcome.action_taken
        refreshed = await _build_card(
            container, _conversation_by_id(conversations, best.conversation_id), outcome.action_taken
        )
        _replace_card(summary, refreshed)
        if refreshed.booking is not None:
            booking = refreshed.booking
            out.print(
                Panel(
                    f"Собеседование назначено: [bold]{_format_slot(booking.start, booking.timezone)}[/bold]\n"
                    f"Встреча в календаре: [dim]{booking.external_meeting_id or '—'}[/dim]\n"
                    "Бронь подтверждена перечитыванием встречи из календаря.",
                    title="[bold green]Готово[/bold green]",
                    border_style="green",
                )
            )
        else:
            out.print("  [yellow]Бронь не создана: слот не подтверждён.[/yellow]")

    # --- Шаг 5. Итоги -------------------------------------------------------
    _heading(out, "5", "Итоги и переписка")
    _print_summary(out, summary)
    await _print_timeline(out, container, summary)

    await container.aclose()
    return summary


# --------------------------------------------------------------------------- #
# Вывод
# --------------------------------------------------------------------------- #


def _print_intro(console: Console, container: Container) -> None:
    console.print(
        Panel.fit(
            "[bold]AI-рекрутер[/bold] — сквозной сценарий на тестовых адаптерах\n"
            f"Каналы: {', '.join(channel.value for channel in container.enabled_channels)}   "
            f"Календарь: {'подключён' if container.calendar else 'нет'}   "
            f"LLM: {container.settings.llm.provider}   "
            f"Очередь: {container.settings.queue.backend}\n"
            "[dim]Сеть и внешние ключи не нужны: подменены адаптеры площадок, календаря и модели.[/dim]",
            border_style="cyan",
        )
    )


def _heading(console: Console, number: str, title: str) -> None:
    console.print()
    console.rule(f"[bold cyan]Шаг {number}.[/bold cyan] {title}", align="left")


def _print_screening(console: Console, card: CandidateCard) -> None:
    if card.score is None:
        console.print(
            f"  [bold]{escape(card.name)}[/bold]: скрининг недоступен "
            f"([yellow]{card.verdict}[/yellow]) | действие: {card.action}"
        )
        return
    console.print(
        f"  [bold]{escape(card.name)}[/bold]: балл [bold]{card.score:.1f}[/bold] "
        f"-> [cyan]{card.verdict}[/cyan] | действие: {card.action}"
    )


def _print_summary(console: Console, summary: DemoSummary) -> None:
    table = Table(show_lines=True)
    table.add_column("Кандидат", style="bold", no_wrap=True)
    table.add_column("Балл", justify="right")
    table.add_column("Вердикт")
    table.add_column("Состояние диалога")
    table.add_column("Собеседование")
    for card in summary.candidates:
        booking = "—" if card.booking is None else _format_slot(card.booking.start, card.booking.timezone)
        table.add_row(
            escape(card.name),
            "—" if card.score is None else f"{card.score:.1f}",
            card.verdict,
            card.state.value,
            booking,
        )
    console.print(table)
    console.print(
        f"  Откликов обработано: [bold]{summary.found}[/bold] | "
        f"диалогов заведено: [bold]{summary.created}[/bold] | "
        f"собеседований назначено: [bold green]{len(summary.booked)}[/bold green]"
    )
    if summary.errors:
        console.print(f"  [yellow]Ошибки обработки: {len(summary.errors)}[/yellow]")
    console.print(
        "\n[dim]По существу показано: отклики собираются идемпотентно, резюме оцениваются "
        "по критериям вакансии с пересчётом балла на нашей стороне, бот пишет по-человечески, "
        "а собеседование попадает в календарь только после подтверждения слота.[/dim]"
    )


async def _print_timeline(console: Console, container: Container, summary: DemoSummary) -> None:
    """Показать переписку по диалогу, в котором назначена встреча."""
    if not summary.booked:
        return
    card = summary.booked[0]
    async with container.uow_factory() as uow:
        messages = list(await uow.messages.list_for_conversation(card.conversation_id, limit=50))

    console.print()
    console.rule(f"[bold cyan]Переписка: {escape(card.name)}[/bold cyan]", align="left")
    for message in messages:
        outgoing = message.direction is MessageDirection.OUTBOUND
        who = "Бот" if outgoing else "Кандидат"
        colour = "green" if outgoing else "white"
        console.print(Text.assemble((f"{who}: ", f"bold {colour}"), (message.body, colour)))


# --------------------------------------------------------------------------- #
# Данные
# --------------------------------------------------------------------------- #


async def _load_conversations(container: Container) -> list[Conversation]:
    async with container.uow_factory() as uow:
        return list(await uow.conversations.list_by_state(list(ConversationState), limit=50))


async def _build_card(container: Container, conversation: Conversation, action: str) -> CandidateCard:
    async with container.uow_factory() as uow:
        # Диалог перечитываем: после шага воронки его состояние изменилось,
        # а объект, прочитанный до обработки, уже устарел.
        fresh = await uow.conversations.get(conversation.id) or conversation
        candidate = await uow.candidates.get(fresh.candidate_id)
        screening: ScreeningResult | None = await uow.screenings.get_latest(
            fresh.candidate_id, fresh.vacancy_id
        )
        booking = await uow.bookings.get_for_conversation(fresh.id)
    return CandidateCard(
        conversation_id=fresh.id,
        name=candidate.full_name if candidate is not None else "—",
        state=fresh.state,
        score=screening.total_score if screening is not None else None,
        verdict=screening.verdict.value if screening is not None else "—",
        action=action,
        booking=booking,
    )


def _replace_card(summary: DemoSummary, refreshed: CandidateCard) -> None:
    for index, card in enumerate(summary.candidates):
        if card.conversation_id == refreshed.conversation_id:
            summary.candidates[index] = refreshed
            return


def _conversation_by_id(conversations: list[Conversation], conversation_id: UUID) -> Conversation:
    return next(item for item in conversations if item.id == conversation_id)


async def _simulate_reply(container: Container, card: CandidateCard, text: str) -> None:
    """Добавить входящее сообщение в чат площадки от имени кандидата.

    Так же, как это произошло бы в жизни: сообщение появляется в чате площадки,
    а бот увидит его следующим шагом воронки.
    """
    async with container.uow_factory() as uow:
        conversation = await uow.conversations.get(card.conversation_id)
        history = await uow.messages.list_for_conversation(card.conversation_id, limit=200)
    if conversation is None:
        return
    channel = container.channels.get(conversation.channel)
    if not isinstance(channel, FakeChannelAdapter):
        return
    channel.add_inbound_message(
        conversation.external_id,
        InboundMessage(
            external_id=f"demo-in-{conversation.external_id}-{len(history)}",
            direction=MessageDirection.INBOUND,
            text=text,
            created_at=_aware(container.clock.now()),
            author_name=card.name,
        ),
    )


def _aware(value: datetime) -> datetime:
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def _format_slot(start: datetime, timezone: str) -> str:
    try:
        local = start.astimezone(ZoneInfo(timezone))
    except Exception:
        local = _aware(start)
    return f"{WEEKDAYS[local.weekday()]}, {local:%d.%m.%Y %H:%M} ({timezone})"


def render_summary_text(summary: DemoSummary) -> str:
    """Краткий текстовый отчёт (используется в тестах)."""
    lines = [
        (
            f"откликов={summary.found} диалогов={summary.created} "
            f"броней={len(summary.booked)} повторно_создано={summary.repeat_created}"
        )
    ]
    for card in summary.candidates:
        score = "—" if card.score is None else f"{card.score:.1f}"
        lines.append(f"{card.name}: {card.verdict} ({score}) -> {card.state.value}")
    return "\n".join(lines)
