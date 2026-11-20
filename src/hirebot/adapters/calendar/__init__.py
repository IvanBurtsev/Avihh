"""Адаптер календаря.

Единственная реализация — Talantix (GraphQL). Порт ``CalendarPort`` намеренно
узкий: сервисы спрашивают занятость и слоты, а не «календарь вообще».
"""

from __future__ import annotations

__all__: list[str] = []
