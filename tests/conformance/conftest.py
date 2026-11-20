"""Фикстуры контрактного набора портов.

Набор параметризован по реестрам реализаций из ``conformance_support``: чтобы
подключить боевой адаптер (после получения доступа к API площадки), достаточно
добавить фабрику в реестр — тесты менять не нужно. Сеть в наборе запрещена:
у боевого адаптера транспорт подменяется моком.
"""

from __future__ import annotations

import pytest
from conformance_support import (
    CALENDAR_IMPLEMENTATIONS,
    CHANNEL_IMPLEMENTATIONS,
    CalendarHarness,
    ChannelHarness,
)


@pytest.fixture(params=sorted(CHANNEL_IMPLEMENTATIONS))
def channel_harness(request: pytest.FixtureRequest) -> ChannelHarness:
    """Реализация ``ChannelPort`` под контрактными тестами."""
    return CHANNEL_IMPLEMENTATIONS[str(request.param)]()


@pytest.fixture(params=sorted(CALENDAR_IMPLEMENTATIONS))
def calendar_harness(request: pytest.FixtureRequest) -> CalendarHarness:
    """Реализация ``CalendarPort`` под контрактными тестами."""
    return CALENDAR_IMPLEMENTATIONS[str(request.param)]()
