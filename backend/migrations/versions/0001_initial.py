"""initial schema

Revision ID: 0001_initial
Revises:
Create Date: 2026-10-23
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op
from pgvector.sqlalchemy import Vector
from sqlalchemy.dialects import postgresql

revision = "0001_initial"
down_revision = None
branch_labels = None
depends_on = None

EMBEDDING_DIM = 1024
UUID = postgresql.UUID(as_uuid=True)
JSONB = postgresql.JSONB(astext_type=sa.Text())


def upgrade() -> None:
    op.execute("CREATE EXTENSION IF NOT EXISTS vector")
    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")

    op.create_table(
        "knowledge_bases",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("name", sa.String(200), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','deleting','deleted')", name="ck_kb_status"
        ),
    )
    op.create_index("ix_kb_owner_status", "knowledge_bases", ["owner_id", "status"])

    op.create_table(
        "documents",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "kb_id",
            UUID,
            sa.ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("title", sa.String(300), nullable=False),
        sa.Column("status", sa.String(24), nullable=False, server_default="active"),
        sa.Column("current_version_id", UUID, nullable=True),
        sa.Column("active_build_id", UUID, nullable=True),
        sa.Column("pending_build_id", UUID, nullable=True),
        sa.Column("generation", sa.Integer, nullable=False, server_default="0"),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "status IN ('active','deleting','deleted')", name="ck_doc_status"
        ),
    )
    op.create_index("ix_doc_kb_status", "documents", ["kb_id", "status"])

    op.create_table(
        "document_versions",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kb_id", UUID, nullable=False),
        sa.Column(
            "document_id",
            UUID,
            sa.ForeignKey("documents.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("version_no", sa.Integer, nullable=False),
        sa.Column("object_key", sa.String(300), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("media_type", sa.String(120), nullable=False),
        sa.Column("byte_size", sa.Integer, nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.UniqueConstraint("document_id", "version_no", name="uq_version_no"),
        sa.UniqueConstraint("kb_id", "sha256", name="uq_kb_sha"),
    )

    op.create_table(
        "index_builds",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "version_id",
            UUID,
            sa.ForeignKey("document_versions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("state", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("chunk_config", JSONB, nullable=True),
        sa.Column("embedding_model", sa.String(120), nullable=False, server_default=""),
        sa.Column("embedding_dim", sa.Integer, nullable=False, server_default=str(EMBEDDING_DIM)),
        sa.Column("warnings", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint(
            "state IN ('queued','running','ready','failed','cancelled','retired')",
            name="ck_build_state",
        ),
    )
    op.create_index("ix_build_version", "index_builds", ["version_id"])

    op.create_table(
        "chunks",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "build_id",
            UUID,
            sa.ForeignKey("index_builds.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("ordinal", sa.Integer, nullable=False),
        sa.Column("text", sa.Text, nullable=False),
        sa.Column("locator", JSONB, nullable=False),
        sa.Column("embedding", Vector(EMBEDDING_DIM), nullable=False),
        sa.Column("tsv", sa.Text, nullable=False),
        sa.UniqueConstraint("build_id", "ordinal", name="uq_chunk_ordinal"),
    )
    op.create_index(
        "ix_chunks_tsv_trgm",
        "chunks",
        ["tsv"],
        postgresql_using="gin",
        postgresql_ops={"tsv": "gin_trgm_ops"},
    )
    op.create_index(
        "ix_chunks_embedding_hnsw",
        "chunks",
        ["embedding"],
        postgresql_using="hnsw",
        postgresql_ops={"embedding": "vector_cosine_ops"},
    )

    op.create_table(
        "jobs",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("resource_id", UUID, nullable=False),
        sa.Column("state", sa.String(24), nullable=False, server_default="queued"),
        sa.Column("stage", sa.String(32), nullable=True),
        sa.Column("attempt", sa.Integer, nullable=False, server_default="0"),
        sa.Column("max_attempts", sa.Integer, nullable=False, server_default="3"),
        sa.Column("available_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("lease_token", UUID, nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error", JSONB, nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.CheckConstraint("kind IN ('ingest','reindex','cleanup')", name="ck_job_kind"),
        sa.CheckConstraint(
            "state IN ('queued','running','succeeded','failed','cancelled')",
            name="ck_job_state",
        ),
    )
    op.create_index("ix_jobs_state_available", "jobs", ["state", "available_at"])
    op.create_index("ix_jobs_lease", "jobs", ["lease_expires_at"])

    op.create_table(
        "conversations",
        sa.Column("id", UUID, primary_key=True),
        sa.Column("owner_id", sa.String(64), nullable=False),
        sa.Column("title", sa.String(200), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )

    op.create_table(
        "messages",
        sa.Column("id", UUID, primary_key=True),
        sa.Column(
            "conversation_id",
            UUID,
            sa.ForeignKey("conversations.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("role", sa.String(16), nullable=False),
        sa.Column("body", sa.Text, nullable=False, server_default=""),
        sa.Column("answer_status", sa.String(32), nullable=False, server_default=""),
        sa.Column("citations", JSONB, nullable=True),
        sa.Column("model_revision", sa.String(64), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
    )
    op.create_index("ix_msg_conversation", "messages", ["conversation_id", "created_at"])


def downgrade() -> None:
    op.drop_table("messages")
    op.drop_table("conversations")
    op.drop_table("jobs")
    op.drop_table("chunks")
    op.drop_table("index_builds")
    op.drop_table("document_versions")
    op.drop_table("documents")
    op.drop_table("knowledge_bases")
