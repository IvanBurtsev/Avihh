"""Проверка, что миграции Alembic соответствуют моделям.

Тест запускает ``alembic upgrade head`` в отдельном процессе (свой event loop
нужен самому Alembic) на временной SQLite-базе и сравнивает получившуюся схему
с ``Base.metadata``.

Зачем это отдельным тестом, если есть ``alembic check``: ``check`` требует
ручного запуска, а расхождение модели и миграции — это ошибка, которая
проявляется только на проде в момент деплоя.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from collections.abc import Iterator
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, inspect

from hirebot.infra.db.models import Base

pytestmark = pytest.mark.integration

REPO_ROOT = Path(__file__).resolve().parents[2]
ALEMBIC_TIMEOUT_S = 180


def _run_alembic(*args: str, dsn: str) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    env["HIREBOT_DATABASE__DSN"] = dsn
    return subprocess.run(
        [sys.executable, "-m", "alembic", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=ALEMBIC_TIMEOUT_S,
        check=False,
    )


@pytest.fixture
def workdir() -> Iterator[Path]:
    """Рабочий каталог теста внутри репозитория.

    Намеренно **не** ``tmp_path``: ``pytest`` создаёт свои каталоги в системном
    TEMP и перечисляет их, что в ограниченных окружениях (песочница, CI с
    урезанным доступом к TEMP) заканчивается отказом в доступе. Свой каталог
    внутри проекта работает везде и убирается за собой.
    """
    path = REPO_ROOT / ".tmp" / f"alembic-{uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=True)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)


@pytest.fixture
def migrated_db(workdir: Path) -> str:
    """Применить миграции к временной SQLite-базе и вернуть путь к файлу."""
    db_path = workdir / "migrated.db"
    result = _run_alembic("upgrade", "head", dsn=f"sqlite+aiosqlite:///{db_path.as_posix()}")
    assert result.returncode == 0, f"alembic upgrade head упал:\n{result.stderr}"
    return db_path.as_posix()


def test_alembic_upgrade_creates_all_tables(migrated_db: str) -> None:
    engine = create_engine(f"sqlite:///{migrated_db}")
    try:
        actual = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()

    assert actual == set(Base.metadata.tables)


def test_alembic_schema_matches_models(migrated_db: str) -> None:
    engine = create_engine(f"sqlite:///{migrated_db}")
    inspector = inspect(engine)
    try:
        for table_name, table in Base.metadata.tables.items():
            columns = {column["name"] for column in inspector.get_columns(table_name)}
            # Сравниваем именно имена: `set(table.columns)` дал бы объекты Column.
            model_columns = {column.name for column in table.columns}
            assert columns == model_columns, f"колонки {table_name} разошлись с моделями"

            model_indexes = {index.name for index in table.indexes}
            db_indexes = {index["name"] for index in inspector.get_indexes(table_name) if index["name"]}
            assert model_indexes <= db_indexes, f"в БД нет индексов {model_indexes - db_indexes}"

            model_uniques = {
                constraint.name
                for constraint in table.constraints
                if constraint.__class__.__name__ == "UniqueConstraint"
            }
            db_uniques = {constraint["name"] for constraint in inspector.get_unique_constraints(table_name)}
            assert model_uniques <= db_uniques, f"в БД нет unique {model_uniques - db_uniques}"
    finally:
        engine.dispose()


def test_alembic_check_reports_no_drift(migrated_db: str) -> None:
    result = _run_alembic("check", dsn=f"sqlite+aiosqlite:///{migrated_db}")

    assert result.returncode == 0, f"модели и миграции разошлись:\n{result.stdout}\n{result.stderr}"
    assert "No new upgrade operations detected" in result.stdout


def test_alembic_downgrade_removes_schema(migrated_db: str) -> None:
    result = _run_alembic("downgrade", "base", dsn=f"sqlite+aiosqlite:///{migrated_db}")

    assert result.returncode == 0, f"alembic downgrade упал:\n{result.stderr}"
    engine = create_engine(f"sqlite:///{migrated_db}")
    try:
        remaining = set(inspect(engine).get_table_names()) - {"alembic_version"}
    finally:
        engine.dispose()
    assert remaining == set()
