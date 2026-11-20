"""Окружение Alembic: асинхронные миграции, DSN из ``Settings``.

Почему DSN берётся из настроек, а не из ``alembic.ini``: конфигурация БД уже
описана в ``hirebot.config`` (включая требование PostgreSQL в продакшене).
Дублировать строку подключения означает рано или поздно применить миграции не к
той базе — самый дорогой вид ошибки в миграциях.

``render_as_batch=True`` для SQLite: без него Alembic не умеет менять колонки
(SQLite не поддерживает ``ALTER COLUMN``), а демо-режим обязан оставаться
рабочим.
"""

from __future__ import annotations

import asyncio
from logging.config import fileConfig

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.ext.asyncio import create_async_engine

from hirebot.config import Settings
from hirebot.infra.db.models import Base

config = context.config

# Логирование Alembic настраивается ini-файлом; в тестах это лишний шум.
if config.config_file_name is not None and config.attributes.get("configure_logger", True):
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def get_database_url() -> str:
    """DSN из настроек приложения (единственный источник правды)."""
    return Settings().database.dsn


def is_sqlite(url: str) -> bool:
    return url.startswith("sqlite")


def run_migrations_offline() -> None:
    """Сгенерировать SQL без подключения к БД (для ревью и ручного применения)."""
    url = get_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        compare_server_default=True,
        render_as_batch=is_sqlite(url),
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Настройка контекста на уже открытом (синхронном) соединении."""
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        compare_server_default=True,
        render_as_batch=connection.dialect.name == "sqlite",
    )
    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    """Применить миграции через асинхронный движок."""
    engine = create_async_engine(get_database_url(), poolclass=pool.NullPool)
    try:
        async with engine.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        await engine.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()