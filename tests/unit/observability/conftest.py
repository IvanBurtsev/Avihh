"""Фикстуры тестов observability.

Метрики живут в реестре модуля, поэтому каждый тест получает свежий набор:
иначе счётчики предыдущего теста влияли бы на ожидаемые значения (а ``counter``
не «обнуляется» — он только растёт).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest

from hirebot.observability.metrics import reset_metrics


@pytest.fixture(autouse=True)
def isolated_metrics() -> Iterator[None]:
    """Свежий реестр метрик до и после каждого теста."""
    reset_metrics()
    yield
    reset_metrics()
