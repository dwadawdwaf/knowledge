"""为新召回路径添加可独立回填的词项索引。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision = "0008_ragflow_terms"
down_revision = "0007_merge_optimization"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "chunk_search_terms",
        sa.Column("chunk_id", postgresql.UUID(as_uuid=True), sa.ForeignKey("chunks.id", ondelete="CASCADE"), primary_key=True),
        sa.Column("terms", postgresql.ARRAY(sa.Text()), nullable=False),
        sa.Column("boosted", postgresql.ARRAY(sa.Text()), nullable=False),
    )
    op.create_index("ix_chunk_search_terms_gin", "chunk_search_terms", ["terms"], postgresql_using="gin")


def downgrade() -> None:
    op.drop_index("ix_chunk_search_terms_gin", table_name="chunk_search_terms")
    op.drop_table("chunk_search_terms")

