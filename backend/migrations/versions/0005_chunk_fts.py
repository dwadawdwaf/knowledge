"""为现有检索文本建立英文全文索引。"""

import sqlalchemy as sa
from alembic import op

revision = "0005_chunk_fts"
down_revision = "0004_chunk_structure"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_index(
        "ix_chunks_tsv_english_fts", "chunks",
        [sa.text("to_tsvector('pg_catalog.english'::regconfig, tsv)")],
        postgresql_using="gin",
    )


def downgrade() -> None:
    op.drop_index("ix_chunks_tsv_english_fts", table_name="chunks")
