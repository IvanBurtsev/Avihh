"""Общие пути и помощники тестов замера качества."""

from __future__ import annotations

from pathlib import Path

#: Корень проекта: tests/unit/evaluation/evaluation_support.py -> ... -> корень.
PROJECT_ROOT = Path(__file__).resolve().parents[3]

#: Эталонная выборка лежит в репозитории: её читает и CI, и человек, который
#: хочет воспроизвести цифры из docs/EVALUATION.md.
DATASET_PATH = PROJECT_ROOT / "data" / "evaluation" / "golden_set.json"
