"""Сквозной прогон демо-сценария и конфигурация логирования.

Демо — это витрина проекта, поэтому оно покрыто тестом: если воронка сломается,
упадёт не только ручной запуск, но и CI. Отдельно проверяется логирование:
``add_logger_name`` несовместим с ``PrintLogger``, и эта ошибка не проявляется,
пока кто-нибудь реально не напишет в лог.
"""

from __future__ import annotations

import io

import structlog
from rich.console import Console

from hirebot.demo import render_summary_text, run_demo
from hirebot.domain.enums import ConversationState
from hirebot.logging_setup import configure_logging, ensure_utf8_streams, get_logger


def _buffer_console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=100, force_terminal=False, highlight=False), buffer


async def test_demo_runs_full_funnel_without_external_services() -> None:
    """Демо проходит от отклика до собеседования, не обращаясь к сети."""
    console, buffer = _buffer_console()

    summary = await run_demo(console=console)

    assert summary.found == 3
    assert summary.created == 3
    assert summary.errors == []
    # Повторный поллинг того же канала не должен создавать новые диалоги.
    assert summary.repeat_created == 0

    best = summary.best
    assert best is not None
    assert best.score is not None
    assert best.score >= 70, "сильный кандидат должен пройти порог приглашения"
    assert best.booking is not None, "слот подтверждён — собеседование обязано быть создано"
    assert best.state is ConversationState.BOOKED

    output = buffer.getvalue()
    assert "Собеседование назначено" in output
    # В выводе не должно быть персональных данных, которых нет в резюме.
    assert "hh-resume-1003" not in output


async def test_demo_degrades_instead_of_failing_on_denied_resume() -> None:
    """Закрытое резюме — штатная ситуация: диалог уходит человеку, прогон живёт."""
    console, _ = _buffer_console()

    summary = await run_demo(console=console)

    denied = next(card for card in summary.candidates if card.name == "Иван Кузнецов")
    assert denied.score is None
    assert denied.booking is None
    assert denied.state is ConversationState.HUMAN_REVIEW


async def test_demo_does_not_auto_reject_weak_candidate() -> None:
    """Слабый кандидат уходит на ручное решение, а не получает автоотказ."""
    console, _ = _buffer_console()

    summary = await run_demo(console=console)

    weak = next(card for card in summary.candidates if card.name == "Мария Смирнова")
    assert weak.state is ConversationState.HUMAN_REVIEW
    assert weak.booking is None


def test_render_summary_text_is_compact_and_stable() -> None:
    from uuid import uuid4

    from hirebot.demo import CandidateCard, DemoSummary

    summary = DemoSummary(found=2, created=2)
    summary.candidates.append(
        CandidateCard(
            conversation_id=uuid4(),
            name="Тест Тестов",
            state=ConversationState.BOOKED,
            score=88.5,
            verdict="strong_match",
        )
    )

    text = render_summary_text(summary)

    assert "откликов=2" in text
    assert "Тест Тестов" in text
    assert "88.5" in text


def test_configure_logging_writes_through_stdlib_factory() -> None:
    """Регрессия: процессор ``add_logger_name`` падал на ``PrintLogger``."""
    original = structlog.get_config()
    try:
        configure_logging(level="INFO", json_output=True)
        logger = get_logger("hirebot.test")
        # Вызов не должен бросить AttributeError: 'PrintLogger' has no attribute 'name'.
        logger.info("проверка_логирования", answer=42)
    finally:
        structlog.configure(**original)


def test_ensure_utf8_streams_is_idempotent() -> None:
    ensure_utf8_streams()
    ensure_utf8_streams()


def test_demo_command_is_registered() -> None:
    """Команда ``demo`` должна быть доступна в CLI (её видит проверяющий)."""
    from hirebot.cli import app

    # Typer не заполняет CommandInfo.name, если имя не задано явно:
    # фактическое имя команды берётся из имени функции-обработчика.
    names = {
        command.name or (command.callback.__name__ if command.callback else "")
        for command in app.registered_commands
    }
    assert "demo" in names
    assert {"doctor", "poll", "serve", "worker"} <= names
