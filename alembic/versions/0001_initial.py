"""Начальная схема hirebot.

Revision ID: 0001_initial
Revises:
Create Date: 2026-11-20 09:01:33.000000+00:00

Схема сгенерирована по моделям ``hirebot.infra.db.models`` (``alembic revision
--autogenerate``) и должна им соответствовать. Расхождение ловится командой
``alembic check`` и интеграционным тестом ``tests/integration/test_alembic_migration.py``.

Что важно в этой ревизии:

* имена всех constraint-ов заданы явно — без них PostgreSQL не даст их изменить
  в следующей миграции;
* ``sa.JSON().with_variant(JSONB, "postgresql")`` — на PostgreSQL создаётся
  ``JSONB``, на SQLite (демо, тесты) — обычный ``JSON``;
* ПДн-колонки (``candidates.phone``/``email``, ``resumes.raw_text``) объявлены
  как текст: в них лежит шифротекст, рядом — blind index-ы для поиска.
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

# Идентификаторы ревизии, используются Alembic.
revision: str = "0001_initial"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def _json() -> sa.JSON:
    """JSON-колонка: ``JSONB`` на PostgreSQL, ``JSON`` на остальных диалектах."""
    return sa.JSON().with_variant(postgresql.JSONB(astext_type=sa.Text()), "postgresql")


def upgrade() -> None:
    """Создать начальную схему."""
    op.create_table(
        "companies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("name", sa.String(length=255), nullable=False),
        sa.Column("slug", sa.String(length=120), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_companies")),
        sa.UniqueConstraint("slug", name=op.f("uq_companies_slug")),
    )

    op.create_table(
        "channel_accounts",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_account_id", sa.String(length=128), nullable=True),
        # Шифротекст (Fernet): открытые токены в БД не попадают.
        sa.Column("access_token", sa.Text(), nullable=True),
        sa.Column("refresh_token", sa.Text(), nullable=True),
        sa.Column("token_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_channel_accounts_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_channel_accounts")),
        sa.UniqueConstraint(
            "company_id",
            "channel",
            "external_account_id",
            name="uq_channel_accounts_company_channel_account",
        ),
    )
    op.create_index(
        "ix_channel_accounts_company_channel",
        "channel_accounts",
        ["company_id", "channel"],
        unique=False,
    )

    op.create_table(
        "vacancies",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("title", sa.String(length=512), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("is_active", sa.Boolean(), nullable=False),
        sa.Column("criteria", _json(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_vacancies_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_vacancies")),
        sa.UniqueConstraint("channel", "external_id", name="uq_vacancies_channel_external_id"),
    )
    op.create_index("ix_vacancies_company_active", "vacancies", ["company_id", "is_active"])

    op.create_table(
        "candidates",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("full_name", sa.String(length=512), nullable=False),
        sa.Column("phone", sa.Text(), nullable=True),
        sa.Column("email", sa.Text(), nullable=True),
        # Blind index: HMAC от нормализованного телефона/почты, только для поиска.
        sa.Column("phone_index", sa.String(length=64), nullable=True),
        sa.Column("email_index", sa.String(length=64), nullable=True),
        sa.Column("city", sa.String(length=128), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_candidates_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_candidates")),
        sa.UniqueConstraint("channel", "external_id", name="uq_candidates_channel_external_id"),
    )
    op.create_index("ix_candidates_company", "candidates", ["company_id"])
    op.create_index("ix_candidates_email_index", "candidates", ["email_index"])
    op.create_index("ix_candidates_phone_index", "candidates", ["phone_index"])

    op.create_table(
        "resumes",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        # Шифротекст: текст резюме — это ПДн в чистом виде.
        sa.Column("raw_text", sa.Text(), nullable=False),
        sa.Column("parsed", _json(), nullable=False),
        sa.Column("source_url", sa.String(length=2048), nullable=True),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("fetched_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name=op.f("fk_resumes_candidate_id_candidates"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_resumes")),
    )
    op.create_index("ix_resumes_candidate_fetched", "resumes", ["candidate_id", "fetched_at"])
    op.create_index("ix_resumes_content_hash", "resumes", ["content_hash"])

    op.create_table(
        "conversations",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("company_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("vacancy_id", sa.Uuid(), nullable=False),
        sa.Column("channel", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=False),
        sa.Column("state", sa.String(length=32), nullable=False),
        sa.Column("auto_messages_sent", sa.Integer(), nullable=False),
        sa.Column("last_inbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_outbound_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_action_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name=op.f("fk_conversations_candidate_id_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["company_id"],
            ["companies.id"],
            name=op.f("fk_conversations_company_id_companies"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
            name=op.f("fk_conversations_vacancy_id_vacancies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_conversations")),
        sa.UniqueConstraint("channel", "external_id", name="uq_conversations_channel_external_id"),
    )
    op.create_index("ix_conversations_candidate", "conversations", ["candidate_id"])
    op.create_index(
        "ix_conversations_state_next_action_at", "conversations", ["state", "next_action_at"]
    )
    op.create_index("ix_conversations_vacancy", "conversations", ["vacancy_id"])

    op.create_table(
        "messages",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("direction", sa.String(length=16), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("external_id", sa.String(length=128), nullable=True),
        sa.Column("generated_by_llm", sa.Boolean(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=True),
        sa.Column("prompt_version", sa.String(length=32), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_messages_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_messages")),
        sa.UniqueConstraint(
            "conversation_id", "external_id", name="uq_messages_conversation_external_id"
        ),
    )
    op.create_index("ix_messages_conversation_created", "messages", ["conversation_id", "created_at"])

    op.create_table(
        "screenings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("resume_id", sa.Uuid(), nullable=False),
        sa.Column("vacancy_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("total_score", sa.Float(), nullable=False),
        sa.Column("verdict", sa.String(length=32), nullable=False),
        sa.Column("criteria", _json(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("gaps", _json(), nullable=False),
        sa.Column("strengths", _json(), nullable=False),
        sa.Column("questions", _json(), nullable=False),
        sa.Column("model", sa.String(length=128), nullable=False),
        sa.Column("prompt_version", sa.String(length=32), nullable=False),
        sa.Column("cache_hit", sa.Boolean(), nullable=False),
        sa.Column("content_hash", sa.String(length=64), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name=op.f("fk_screenings_candidate_id_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["resume_id"],
            ["resumes.id"],
            name=op.f("fk_screenings_resume_id_resumes"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
            name=op.f("fk_screenings_vacancy_id_vacancies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_screenings")),
    )
    op.create_index(
        "ix_screenings_candidate_vacancy_created",
        "screenings",
        ["candidate_id", "vacancy_id", "created_at"],
    )
    op.create_index("ix_screenings_content_hash", "screenings", ["content_hash"])
    op.create_index("ix_screenings_resume", "screenings", ["resume_id"])

    op.create_table(
        "bookings",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("conversation_id", sa.Uuid(), nullable=False),
        sa.Column("candidate_id", sa.Uuid(), nullable=False),
        sa.Column("vacancy_id", sa.Uuid(), nullable=False),
        sa.Column("manager_id", sa.String(length=128), nullable=False),
        # Колонки названы starts_at/ends_at: "end" — зарезервированное слово SQL.
        sa.Column("starts_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ends_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("timezone", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("external_meeting_id", sa.String(length=128), nullable=True),
        sa.Column("external_event_id", sa.String(length=128), nullable=True),
        sa.Column("idempotency_key", sa.String(length=255), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["candidate_id"],
            ["candidates.id"],
            name=op.f("fk_bookings_candidate_id_candidates"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["conversation_id"],
            ["conversations.id"],
            name=op.f("fk_bookings_conversation_id_conversations"),
            ondelete="CASCADE",
        ),
        sa.ForeignKeyConstraint(
            ["vacancy_id"],
            ["vacancies.id"],
            name=op.f("fk_bookings_vacancy_id_vacancies"),
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_bookings")),
        sa.UniqueConstraint("idempotency_key", name="uq_bookings_idempotency_key"),
    )
    op.create_index("ix_bookings_candidate", "bookings", ["candidate_id"])
    op.create_index("ix_bookings_conversation", "bookings", ["conversation_id"])
    op.create_index(
        "ix_bookings_manager_window", "bookings", ["manager_id", "starts_at", "ends_at"]
    )

    op.create_table(
        "outbox",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("topic", sa.String(length=128), nullable=False),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("dedupe_key", sa.String(length=255), nullable=False),
        sa.Column("status", sa.String(length=16), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_outbox")),
        sa.UniqueConstraint("dedupe_key", name="uq_outbox_dedupe_key"),
    )
    op.create_index("ix_outbox_status_available_at", "outbox", ["status", "available_at"])

    op.create_table(
        "audit_log",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("actor", sa.String(length=128), nullable=False),
        sa.Column("action", sa.String(length=128), nullable=False),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=True),
        sa.Column("payload", _json(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("correlation_id", sa.String(length=64), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_audit_log")),
    )
    op.create_index("ix_audit_log_created_at", "audit_log", ["created_at"])
    op.create_index("ix_audit_log_entity", "audit_log", ["entity_type", "entity_id"])

    op.create_table(
        "idempotency_keys",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("key", sa.String(length=255), nullable=False),
        sa.Column("scope", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.PrimaryKeyConstraint("id", name=op.f("pk_idempotency_keys")),
        sa.UniqueConstraint("key", "scope", name="uq_idempotency_keys_key_scope"),
    )
    op.create_index("ix_idempotency_keys_expires_at", "idempotency_keys", ["expires_at"])


def downgrade() -> None:
    """Откатить схему (в обратном порядке, чтобы не мешали внешние ключи)."""
    op.drop_index("ix_idempotency_keys_expires_at", table_name="idempotency_keys")
    op.drop_table("idempotency_keys")

    op.drop_index("ix_audit_log_entity", table_name="audit_log")
    op.drop_index("ix_audit_log_created_at", table_name="audit_log")
    op.drop_table("audit_log")

    op.drop_index("ix_outbox_status_available_at", table_name="outbox")
    op.drop_table("outbox")

    op.drop_index("ix_bookings_manager_window", table_name="bookings")
    op.drop_index("ix_bookings_conversation", table_name="bookings")
    op.drop_index("ix_bookings_candidate", table_name="bookings")
    op.drop_table("bookings")

    op.drop_index("ix_screenings_resume", table_name="screenings")
    op.drop_index("ix_screenings_content_hash", table_name="screenings")
    op.drop_index("ix_screenings_candidate_vacancy_created", table_name="screenings")
    op.drop_table("screenings")

    op.drop_index("ix_messages_conversation_created", table_name="messages")
    op.drop_table("messages")

    op.drop_index("ix_conversations_vacancy", table_name="conversations")
    op.drop_index("ix_conversations_state_next_action_at", table_name="conversations")
    op.drop_index("ix_conversations_candidate", table_name="conversations")
    op.drop_table("conversations")

    op.drop_index("ix_resumes_content_hash", table_name="resumes")
    op.drop_index("ix_resumes_candidate_fetched", table_name="resumes")
    op.drop_table("resumes")

    op.drop_index("ix_candidates_phone_index", table_name="candidates")
    op.drop_index("ix_candidates_email_index", table_name="candidates")
    op.drop_index("ix_candidates_company", table_name="candidates")
    op.drop_table("candidates")

    op.drop_index("ix_vacancies_company_active", table_name="vacancies")
    op.drop_table("vacancies")

    op.drop_index("ix_channel_accounts_company_channel", table_name="channel_accounts")
    op.drop_table("channel_accounts")

    op.drop_table("companies")
