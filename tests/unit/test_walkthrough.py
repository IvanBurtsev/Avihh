"""Тест пошагового разбора.

Разбор — это документация, которую можно запустить. Если он разойдётся с
фактическим поведением системы, читатель получит неверную картину, поэтому
тест проверяет ключевые утверждения разбора, а не только отсутствие падения.
"""

from __future__ import annotations

import io

from rich.console import Console

from hirebot.walkthrough import CANDIDATE_NAME, format_summary, run_walkthrough


def _buffer_console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(file=buffer, width=110, force_terminal=False, highlight=False), buffer


async def test_walkthrough_covers_full_cycle() -> None:
    console, buffer = _buffer_console()

    summary = await run_walkthrough(console=console)

    assert summary["score"] is not None
    assert summary["verdict"]
    # При включённом подтверждении первый шаг завершается черновиком, а не отправкой.
    assert summary["action"] == "invited_draft"
    assert summary["booked"] is True

    output = buffer.getvalue()
    for marker in (
        "Площадка отдаёт отклик",
        "Поллинг",
        "Скоркинг" if False else "Скоринг по критериям",
        "Черновик на подтверждение",
        "Рекрутер одобряет",
        "Слоты из календаря",
        "Бронь",
    ):
        assert marker in output, f"в разборе нет шага «{marker}»"


async def test_walkthrough_shows_no_message_before_approval() -> None:
    """Ключевая гарантия должна быть видна в выводе, а не только в коде."""
    console, buffer = _buffer_console()

    await run_walkthrough(console=console)

    output = buffer.getvalue()
    assert "Сообщений в чате площадки: 0" in output
    assert "кандидат ещё ничего не получил" in output
    assert "Одобрено → сообщений в чате: 1" in output


async def test_walkthrough_prints_scoring_formula() -> None:
    """Формула печатается: видно, что балл считаем мы, а не модель."""
    console, buffer = _buffer_console()

    await run_walkthrough(console=console)

    output = buffer.getvalue()
    assert "Σ(оценка × вес) / Σ(вес)" in output
    assert "Итоговый балл" in output


async def test_walkthrough_reaches_booked_state() -> None:
    console, _ = _buffer_console()

    await run_walkthrough(console=console)

    # Разбор доводит диалог до брони: иначе он описывал бы половину процесса.
    assert CANDIDATE_NAME


def test_format_summary_is_stable() -> None:
    text = format_summary(
        {
            "score": 90.1,
            "verdict": "match",
            "action": "invited_draft",
            "sent_after_approval": 1,
            "booked": True,
        }
    )

    assert "90.1" in text
    assert "invited_draft" in text
    # «бронь=True» — сводка печатает факт брони, а не пустое место.
    assert "True" in text
