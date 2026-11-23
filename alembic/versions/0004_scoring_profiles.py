"""Профиль скоринга: критерии, вопросы, пороги и версии.

Revision ID: 0004_scoring_profiles
Revises: 0003_message_drafts
Create Date: 2026-11-23

Что делаем:

1. Создаём три таблицы: ``scoring_profiles``, ``profile_criteria``,
   ``profile_questions``.
2. Расширяем ``screenings`` полями ``percent``, ``coverage``, ``blockers``,
   ``profile_version`` — чтобы решение было объяснимо задним числом.
3. **Переносим существующие критерии**: у вакансий уже есть ``criteria`` в
   старом формате, и они должны продолжать работать. Для каждой такой вакансии
   создаётся профиль версии 1 со статусом ``active`` и её критериями.

Почему статус ``active``, а не ``shadow``: это данные, которые уже
использовались в работе. Понижение до shadow изменило бы поведение молча.
"""

from __future__ import annotations

import json
from collections.abc import Sequence
from typing import Any
from uuid import uuid4

import sqlalchemy as sa
from alembic import op

revision: str = "0004_scoring_profiles"
down_revision: str | None = "0003_message_drafts"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

#: Пороги по умолчанию совпадают с ``ProfileThresholds`` в домене.
DEFAULT_INVITE_AT = 85.0
DEFAULT_HUMAN_AT = 55.0
DEFAULT_REJECT_AT = 40.0
DEFAULT_TOP_K = 10
DEFAULT_MAX_QUESTIONS = 5
DEFAULT_MIN_COVERAGE = 0.7


def upgrade() -> None:
    op.create_table(
        "scoring_profiles",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("vacancy_id", sa.Uuid(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("invite_at", sa.Float(), nullable=False),
        sa.Column("human_at", sa.Float(), nullable=False),
        sa.Column("reject_at", sa.Float(), nullable=False),
        sa.Column("threshold_mode", sa.String(length=16), nullable=False),
        sa.Column("top_k", sa.Integer(), nullable=False),
        sa.Column("max_questions", sa.Integer(), nullable=False),
        sa.Column("auto_invite", sa.Boolean(), nullable=False),
        sa.Column("min_coverage_for_auto", sa.Float(), nullable=False),
        sa.Column("created_by", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name="fk_scoring_profiles_company_id_companies",
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
            name="fk_scoring_profiles_vacancy_id_vacancies",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_scoring_profiles"),
        sa.UniqueConstraint("vacancy_id", "version", name="uq_scoring_profiles_vacancy_version"),
    )
    op.create_index(
        "uq_scoring_profiles_vacancy_live",
        "scoring_profiles",
        ["vacancy_id"],
        unique=True,
        sqlite_where=sa.text("status IN ('active', 'shadow')"),
        postgresql_where=sa.text("status IN ('active', 'shadow')"),
    )
    op.create_index(
        "ix_scoring_profiles_vacancy_status",
        "scoring_profiles",
        ["vacancy_id", "status"],
        unique=False,
    )

    op.create_table(
        "profile_criteria",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=64), nullable=False),
        sa.Column("title", sa.String(length=255), nullable=False),
        sa.Column("weight", sa.Integer(), nullable=False),
        sa.Column("must_have", sa.Boolean(), nullable=False),
        sa.Column("evidence_hint", sa.Text(), nullable=False),
        sa.Column("expectation", sa.Text(), nullable=False),
        sa.Column("source", sa.String(length=16), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["scoring_profiles.id"],
            name="fk_profile_criteria_profile_id_scoring_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_profile_criteria"),
        sa.UniqueConstraint("profile_id", "key", name="uq_profile_criteria_profile_key"),
    )
    op.create_index(
        "ix_profile_criteria_profile_position",
        "profile_criteria",
        ["profile_id", "position"],
        unique=False,
    )

    op.create_table(
        "profile_questions",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("profile_id", sa.Uuid(), nullable=False),
        sa.Column("criterion_key", sa.String(length=64), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=16), nullable=False),
        sa.Column("informativeness", sa.Integer(), nullable=False),
        sa.Column("is_custom", sa.Boolean(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["profile_id"],
            ["scoring_profiles.id"],
            name="fk_profile_questions_profile_id_scoring_profiles",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name="pk_profile_questions"),
    )
    op.create_index(
        "ix_profile_questions_profile_position",
        "profile_questions",
        ["profile_id", "position"],
        unique=False,
    )

    # Расширение screenungs: объяснимость решения.
    with op.batch_alter_table("screenings") as batch:
        batch.add_column(sa.Column("percent", sa.Float(), nullable=True))
        batch.add_column(sa.Column("coverage", sa.Float(), nullable=True))
        batch.add_column(
            sa.Column("blockers", sa.JSON(), nullable=False, server_default=sa.text("'[]'"))
        )
        batch.add_column(sa.Column("profile_version", sa.Integer(), nullable=True))

    _migrate_existing_criteria()

    # server_default нужен только для существующих строк; модель задаёт default.
    with op.batch_alter_table("screenings") as batch:
        batch.alter_column("blockers", server_default=None)


def _migrate_existing_criteria() -> None:
    """Превратить старые ``vacancies.criteria`` в профили версии 1.

    Уже работающие вакансии не должны остаться без профиля: иначе после
    обновления их кандидаты получат «нет критериев» вместо прежней оценки.
    """
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, company_id, criteria FROM vacancies")
    ).fetchall()

    for row in rows:
        raw = _as_list(row.criteria)
        if not raw:
            # Вакансия без критериев: профиль не создаём — она и раньше
            # отправляла кандидатов человеку.
            continue

        profile_id = uuid4()
        bind.execute(
            sa.text(
                """
                INSERT INTO scoring_profiles (
                    id, company_id, vacancy_id, version, status,
                    invite_at, human_at, reject_at, threshold_mode, top_k,
                    max_questions, auto_invite, min_coverage_for_auto,
                    created_by, created_at, published_at, comment
                ) VALUES (
                    :id, :company_id, :vacancy_id, 1, 'active',
                    :invite_at, :human_at, :reject_at, 'absolute', :top_k,
                    :max_questions, :auto_invite, :min_coverage,
                    'migration', :created_at, :created_at, :comment
                )
                """
            ),
            {
                "id": profile_id.hex if _uses_hex_uuid(bind) else profile_id,
                "company_id": _uuid_value(bind, row.company_id),
                "vacancy_id": _uuid_value(bind, row.id),
                "invite_at": DEFAULT_INVITE_AT,
                "human_at": DEFAULT_HUMAN_AT,
                "reject_at": DEFAULT_REJECT_AT,
                "top_k": DEFAULT_TOP_K,
                "max_questions": DEFAULT_MAX_QUESTIONS,
                "auto_invite": False,
                "min_coverage": DEFAULT_MIN_COVERAGE,
                "created_at": sa.func.now(),
                "comment": "Профиль создан автоматически из прежних критериев вакансии",
            },
        )

        for position, item in enumerate(raw):
            if not isinstance(item, dict):
                continue
            bind.execute(
                sa.text(
                    """
                    INSERT INTO profile_criteria (
                        id, profile_id, key, title, weight, must_have,
                        evidence_hint, expectation, source, position
                    ) VALUES (
                        :id, :profile_id, :key, :title, :weight, :must_have,
                        :evidence_hint, :expectation, 'catalog', :position
                    )
                    """
                ),
                {
                    "id": uuid4().hex if _uses_hex_uuid(bind) else uuid4(),
                    "profile_id": profile_id.hex if _uses_hex_uuid(bind) else profile_id,
                    "key": str(item.get("key") or f"criterion_{position}"),
                    "title": str(item.get("title") or item.get("key") or "Критерий"),
                    "weight": int(item.get("weight") or 1),
                    "must_have": bool(item.get("must_have")),
                    "evidence_hint": str(item.get("evidence_hint") or ""),
                    "expectation": str(item.get("expectation") or item.get("title") or ""),
                    "position": position,
                },
            )


def _as_list(value: Any) -> list[Any]:
    """Прочитать JSON-колонку, которая в SQLite может быть строкой."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, str):
        try:
            parsed = json.loads(value)
        except json.JSONDecodeError:
            return []
        return parsed if isinstance(parsed, list) else []
    return []


def _uses_hex_uuid(bind: Any) -> bool:
    """SQLite хранит ``Uuid`` строкой без дефисов, PostgreSQL — типом uuid."""
    return bind.dialect.name == "sqlite"


def _uuid_value(bind: Any, value: Any) -> Any:
    if _uses_hex_uuid(bind):
        return str(value).replace("-", "")
    return value


def downgrade() -> None:
    with op.batch_alter_table("screenings") as batch:
        batch.drop_column("profile_version")
        batch.drop_column("blockers")
        batch.drop_column("coverage")
        batch.drop_column("percent")

    op.drop_index("ix_profile_questions_profile_position", table_name="profile_questions")
    op.drop_table("profile_questions")
    op.drop_index("ix_profile_criteria_profile_position", table_name="profile_criteria")
    op.drop_table("profile_criteria")
    op.drop_index("ix_scoring_profiles_vacancy_status", table_name="scoring_profiles")
    op.drop_index("uq_scoring_profiles_vacancy_live", table_name="scoring_profiles")
    op.drop_table("scoring_profiles")
