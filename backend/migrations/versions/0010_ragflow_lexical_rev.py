"""标记 RAGFlow 派生索引的词项算法版本。"""

import sqlalchemy as sa
from alembic import op

revision = "0010_ragflow_lexical_rev"
down_revision = "0009_ragflow_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunk_search_terms",
        sa.Column("lexical_revision", sa.SmallInteger(), nullable=False, server_default="1"),
    )


def downgrade() -> None:
    op.drop_column("chunk_search_terms", "lexical_revision")
