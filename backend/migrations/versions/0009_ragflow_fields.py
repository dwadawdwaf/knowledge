"""为 RAGFlow 多字段召回单独记录字段词项。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0009_ragflow_fields"
down_revision = "0008_ragflow_terms"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("chunk_search_terms", sa.Column("fields", postgresql.JSONB(), nullable=True))


def downgrade() -> None:
    op.drop_column("chunk_search_terms", "fields")
