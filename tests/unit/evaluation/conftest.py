"""Фикстуры тестов подсистемы замера качества скрининга."""

from __future__ import annotations

import pytest
from evaluation_support import DATASET_PATH

from hirebot.evaluation import GoldenDataset, load_dataset


@pytest.fixture(scope="module")
def dataset() -> GoldenDataset:
    """Загруженная эталонная выборка (одна на модуль: файл не меняется)."""
    return load_dataset(DATASET_PATH)
