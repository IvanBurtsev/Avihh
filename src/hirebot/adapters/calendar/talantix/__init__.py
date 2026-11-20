"""Адаптер календаря Talantix.

Модули:

* ``queries`` — тексты GraphQL (одно корневое поле на операцию, сложность ≤ 2500);
* ``client`` — транспорт ``POST /graphql``, разбор ``errors[]``, single-flight обновление токена;
* ``mapper`` — GraphQL -> ``TalantixMeeting``/``BusyInterval``, сборка input-объектов;
* ``adapter`` — реализация ``CalendarPort``, включая расчёт свободных слотов.
"""

from __future__ import annotations

__all__: list[str] = []
