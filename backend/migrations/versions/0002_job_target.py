"""Bind each ingest attempt to a build and document generation."""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import UUID

revision = "0002_job_target"
down_revision = "0001_initial"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute("""
        DO $$ BEGIN
          IF EXISTS (SELECT 1 FROM jobs WHERE kind IN ('ingest','reindex')
                     AND state IN ('queued','running')) THEN
            RAISE EXCEPTION 'Drain ingest/reindex jobs before migration 0002';
          END IF;
        END $$;
    """)
    op.add_column("jobs", sa.Column("build_id", UUID(as_uuid=True), nullable=True))
    op.add_column("jobs", sa.Column("generation", sa.Integer(), nullable=True))
    op.create_check_constraint(
        "ck_job_target",
        "jobs",
        "kind = 'cleanup' OR state NOT IN ('queued','running') "
        "OR (build_id IS NOT NULL AND generation IS NOT NULL)",
    )


def downgrade() -> None:
    op.drop_constraint("ck_job_target", "jobs", type_="check")
    op.drop_column("jobs", "generation")
    op.drop_column("jobs", "build_id")
