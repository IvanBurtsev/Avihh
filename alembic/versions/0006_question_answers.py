"""Ответы кандидатов на уточняющие вопросы.

Revision ID: 0006_question_answers
Revises: 0005_paid_actions
Create Date: 2026-11-23

Зачем: ответ — это новые данные для скоринга. Без привязки к критерию нельзя
пересчитать соответствие и объяснить, что изменилось после уточнения.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_question_answers"
down_revision: str | None = "0005_paid_actions"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "question_answers",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("criterion_key", sa.String(length=64), nullable=False),
        sa.Column("question", sa.Text(), nullable=False),
        sa.Column("answer", sa.Text(), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("is_unclear", sa.Boolean(), nullable=False),
        sa.Column("contradicts_resume", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name="fk_question_answers_conversation_id_conversations",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_question_answers"),
        sa.UniqueConstraint(
            "conversation_id",
            "criterion_key",
            "question",
            name="uq_question_answers_unique",
        ),
    )
    op.create_index(
        "ix_question_answers_conversation",
        "question_answers",
        ["conversation_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_question_answers_conversation", table_name="question_answers")
    op.drop_table("question_answers")
