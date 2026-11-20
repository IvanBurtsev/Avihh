"""Частичный уникальный индекс активных слотов брони.

Revision ID: 0002_bookings_active_slot_unique
Revises: 0001_initial
Create Date: 2026-11-20 13:10:00.000000+00:00

Зачем индекс: площадка (Talantix) **не валидирует конфликты** и охотно создаёт
пересекающуюся встречу (ARCHITECTURE.md, п. 6.3). Проверка «прочитал занятость —
создал встречу» гонку двух кандидатов на одно время не закрывает, поэтому
последний рубеж — ограничение БД: у одного менеджера не может быть двух активных
броней (``proposed``/``confirmed``) на одно и то же время.

Индекс частичный: ``postgresql_where`` / ``sqlite_where`` (обе СУБД поддерживают
частичные индексы). Отменённые и завершённые брони времени не занимают, иначе
история мешала бы новым бронированиям.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

# Идентификаторы ревизии, используются Alembic.
revision: str = "0002_bookings_active_slot_unique"
down_revision: str | None = "0001_initial"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Индекс и предикат должны буквально совпадать с моделью
#: (``hirebot.infra.db.models.BookingRow``), иначе ``alembic check`` покажет drift.
INDEX_NAME = "uq_bookings_manager_active_slot"
ACTIVE_STATUSES_PREDICATE = "status IN ('proposed', 'confirmed')"


def upgrade() -> None:
    """Создать частичный уникальный индекс активных броней."""
    op.create_index(
        INDEX_NAME,
        "bookings",
        ["manager_id", "starts_at"],
        unique=True,
        postgresql_where=sa.text(ACTIVE_STATUSES_PREDICATE),
        sqlite_where=sa.text(ACTIVE_STATUSES_PREDICATE),
    )


def downgrade() -> None:
    """Удалить индекс (схема возвращается к 0001_initial)."""
    op.drop_index(INDEX_NAME, table_name="bookings")
