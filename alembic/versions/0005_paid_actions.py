"""Журнал платных действий: контроль бюджета.

Revision ID: 0005_paid_actions
Revises: 0004_scoring_profiles
Create Date: 2026-11-23

Зачем: просмотр резюме и приглашение на hh.ru и Avito могут списывать деньги.
Без журнала нельзя ответить «за что ушла квота», а без счётчика — остановить
расход до того, как он случился.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_paid_actions"
down_revision: str | None = "0004_scoring_profiles"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "paid_actions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=16), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("cost", sa.Float(), nullable=False),
        sa.Column("reference", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_paid_actions_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_paid_actions"),
    )
    op.create_index(
        "ix_paid_actions_company_created",
        "paid_actions",
        ["company_id", "created_at"],
        unique=False,
    )
    op.create_index(
        "ix_paid_actions_company_kind",
        "paid_actions",
        ["company_id", "kind"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index("ix_paid_actions_company_kind", table_name="paid_actions")
    op.drop_index("ix_paid_actions_company_created", table_name="paid_actions")
    op.drop_table("paid_actions")
