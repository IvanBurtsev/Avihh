"""Адаптер канала hh.ru.

Модули:

* ``auth`` — OAuth-токены (3-legged вход, single-flight обновление);
* ``client`` — низкоуровневые вызовы Employer API через ``HttpClientPort``;
* ``mapper`` — JSON -> DTO (в т.ч. нормализация резюме в плоский текст);
* ``adapter`` — реализация ``ChannelPort``.
"""

from __future__ import annotations

__all__: list[str] = []
