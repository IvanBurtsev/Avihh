"""Черновики сообщений: очередь подтверждения рекрутером.

Revision ID: 0003_message_drafts
Revises: 0002_bookings_active_slot_unique
Create Date: 2026-11-20

Зачем таблица: бот не должен писать реальным кандидатам без человека, но и
молчать не должен. Черновик — компромисс: бот решает, что ответить, и готовит
текст; отправка происходит после одобрения.

Частичный уникальный индекс ``uq_message_drafts_conversation_pending``
гарантирует на уровне БД, что у диалога не больше одного ожидающего черновика:
иначе рекрутер мог бы одобрить устаревшее сообщение.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_message_drafts"
down_revision: str | None = "0002_bookings_active_slot_unique"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "message_drafts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("target_state", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("generated_by_llm", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("reviewer", sa.String(length=120), nullable=True),
        sa.Column("review_comment", sa.Text(), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_message_drafts_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_message_drafts"),
    )
    op.create_index(
        "uq_message_drafts_conversation_pending",
        "message_drafts",
        ["conversation_id"],
        unique=True,
        sqlite_where=sa.text("status = 'pending'"),
        postgresql_where=sa.text("status = 'pending'"),
    )
    op.create_index(
        "ix_message_drafts_status_created",
        "message_drafts",
        ["status", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_message_drafts_status_created", table_name="message_drafts")
    op.drop_index("uq_message_drafts_conversation_pending", table_name="message_drafts")
    op.drop_table("message_drafts")
