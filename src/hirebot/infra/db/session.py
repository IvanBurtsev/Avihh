"""Сборка слоя доступа к данным: движок, фабрика сессий, фабрика UoW.

Почему всё это живёт в одном модуле: движок, пул соединений и фабрика сессий
должны быть согласованы. Если каждый сервис создаст свой движок, приложение
получит N пулов соединений и исчерпание ``max_connections`` у PostgreSQL — и это
самый частый способ «положить» прод на пустом месте.

Параметры пула тоже задаются здесь: ``pool_size``/``max_overflow`` для PostgreSQL
и ``StaticPool`` для SQLite в памяти (иначе каждый коннект — это новая пустая
база, и тесты «теряют» данные между запросами).
"""

from __future__ import annotations

from contextlib import AbstractAsyncContextManager
from typing import TYPE_CHECKING, Any

from sqlalchemy import event, inspect
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.pool import StaticPool

from hirebot.config import Settings
from hirebot.infra.db.models import Base
from hirebot.infra.db.uow import SqlAlchemyUnitOfWork
from hirebot.infra.security.crypto import FieldCipher
from hirebot.logging_setup import get_logger
from hirebot.ports.repositories import UnitOfWork

if TYPE_CHECKING:
    from hirebot.container import UowFactory

logger = get_logger(__name__)


def _is_memory_sqlite(dsn: str) -> bool:
    """SQLite в памяти: ``sqlite+aiosqlite:///:memory:`` или ``sqlite://``."""
    if not dsn.startswith("sqlite"):
        return False
    return ":memory:" in dsn or dsn.rstrip("/").endswith("sqlite:")


def create_engine(settings: Settings) -> AsyncEngine:
    """Создать асинхронный движок по настройкам.

    PostgreSQL: пул нужного размера + ``statement_timeout`` на уровне сервера
    (запрос не должен держать транзакцию бесконечно — иначе поллер заблокирует
    горячие строки диалогов).
    SQLite: в памяти обязателен ``StaticPool``, иначе у каждой сессии своя база.
    """
    dsn = settings.database.dsn
    kwargs: dict[str, Any] = {
        "echo": settings.database.echo_sql,
        # Проверять соединение перед выдачей из пула: после рестарта PostgreSQL
        # «мёртвые» соединения иначе выстреливают ошибкой в первом же запросе.
        "pool_pre_ping": True,
    }
    if settings.database.is_postgres:
        kwargs["pool_size"] = settings.database.pool_size
        kwargs["max_overflow"] = settings.database.max_overflow
        kwargs["connect_args"] = {
            "server_settings": {
                "statement_timeout": str(settings.database.statement_timeout_ms),
            }
        }
    elif _is_memory_sqlite(dsn):
        kwargs["poolclass"] = StaticPool
    engine = create_async_engine(dsn, **kwargs)
    if not settings.database.is_postgres:
        _enable_sqlite_foreign_keys(engine)
    logger.info(
        "db_engine_created",
        dialect=engine.dialect.name,
        postgres=settings.database.is_postgres,
    )
    return engine


def _enable_sqlite_foreign_keys(engine: AsyncEngine) -> None:
    """Включить проверку внешних ключей в SQLite.

    SQLite по умолчанию **не** проверяет FK. Тогда демо и тесты ведут себя не
    так, как продакшен: нарушение ссылочной целостности и ``ON DELETE CASCADE``
    обнаружились бы только на PostgreSQL. Включаем принудительно.

    Управление транзакциями намеренно оставлено драйверу ``pysqlite``: попытка
    выдать ``BEGIN`` вручную приводит к двойному открытию транзакции
    (``cannot start a transaction within a transaction``), а перевод соединения
    в режим без неявных транзакций ломает откат. Особенность SQLite учтена там,
    где она действительно важна, — в работе с вложенными транзакциями
    (см. ``SqlOutboxRepository.add``).
    """

    @event.listens_for(engine.sync_engine, "connect")
    def _set_pragma(dbapi_connection: object, _record: object) -> None:
        cursor = dbapi_connection.cursor()  # type: ignore[attr-defined]
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.close()


def create_sessionmaker(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Фабрика сессий.

    ``expire_on_commit=False`` важен для UoW: после ``commit()`` доменные
    объекты должны оставаться читаемыми, иначе обращение к ним вне транзакции
    сходит в БД (в async-коде это ``MissingGreenlet``).
    ``autoflush=False`` — flush делают репозитории явно, чтобы ошибки
    целостности всплывали в конкретном месте, а не в случайном запросе.
    """
    return async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autoflush=False,
    )


async def init_models(engine: AsyncEngine) -> None:
    """Создать схему по моделям (SQLite-демо и интеграционные тесты).

    В продакшене схему создаёт Alembic; здесь ``create_all`` нужен, чтобы демо
    запускалось одной командой и тесты не зависели от миграций.
    """
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.create_all)
    logger.info("db_schema_ready", tables=len(Base.metadata.tables))


async def drop_models(engine: AsyncEngine) -> None:
    """Удалить схему (только для тестов и пересоздания демо-базы)."""
    async with engine.begin() as connection:
        await connection.run_sync(Base.metadata.drop_all)


async def schema_is_ready(engine: AsyncEngine) -> bool:
    """Есть ли в базе основная таблица (по ней судим о готовности схемы)."""
    probe = "conversations"
    async with engine.connect() as connection:
        return bool(await connection.run_sync(lambda sync_conn: inspect(sync_conn).has_table(probe)))


async def ensure_local_schema(settings: Settings) -> bool:
    """Создать схему для локального запуска, если её ещё нет.

    Зачем: ``hirebot serve`` на чистой машине не должен падать с ``500
    no such table`` на первом же запросе к интерфейсу. Для файловой SQLite схема
    создаётся автоматически — это безопасно и идемпотентно.

    Для PostgreSQL схема **не создаётся**: там единственный законный способ —
    миграции. Отсутствие таблиц означает, что забыли ``alembic upgrade head``,
    и об этом пишется предупреждение, а не молчаливое исправление.

    Возвращает ``True``, если схема была создана этим вызовом.
    """
    if settings.database.is_postgres:
        if settings.is_local:
            logger.warning(
                "db_schema_not_managed",
                hint="для PostgreSQL схему создаёт Alembic: alembic upgrade head",
            )
        return False
    if _is_memory_sqlite(settings.database.dsn):
        # В памяти схему создаёт владелец движка (тесты, демо): отдельный движок
        # здесь открыл бы другую, пустую базу.
        return False

    engine = create_engine(settings)
    try:
        if await schema_is_ready(engine):
            return False
        logger.info("db_schema_creating", reason="схема отсутствует в локальной базе")
        await init_models(engine)
        return True
    finally:
        await engine.dispose()


def create_uow_factory(
    settings: Settings,
    *,
    sessionmaker: async_sessionmaker[AsyncSession] | None = None,
    cipher: FieldCipher | None = None,
) -> UowFactory:
    """Создать фабрику Unit of Work.

    Возвращается **обычная** функция: каждый вызов даёт новую транзакционную
    границу. Движок и фабрика сессий создаются один раз на эту фабрику.

    ``sessionmaker``/``cipher`` можно передать явно — так интеграционные тесты
    используют один движок на тест, а не создают новый пул на каждый вызов.
    """
    cipher_instance = cipher if cipher is not None else FieldCipher.from_settings(settings)
    if sessionmaker is not None:
        session_factory = sessionmaker
    else:
        engine = create_engine(settings)
        session_factory = create_sessionmaker(engine)

    def factory() -> AbstractAsyncContextManager[UnitOfWork]:
        return SqlAlchemyUnitOfWork(session_factory, cipher_instance)

    return factory


__all__ = [
    "create_engine",
    "create_sessionmaker",
    "create_uow_factory",
    "drop_models",
    "ensure_local_schema",
    "init_models",
    "schema_is_ready",
]
