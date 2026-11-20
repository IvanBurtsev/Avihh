"""Адаптеры каналов (площадок).

Каждый подпакет — одна площадка. Все они реализуют ``hirebot.ports.channels.ChannelPort``,
поэтому сервисы не знают, откуда пришёл кандидат.
"""

from __future__ import annotations

__all__: list[str] = []
