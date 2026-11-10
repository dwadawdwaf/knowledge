"""保存新构建的结构关系，已有片段保持原文、向量和定位不变。"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0004_chunk_structure"
down_revision = "0003_chunk_warnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunks", sa.Column("structure", JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb"))
    )


def downgrade() -> None:
    op.drop_column("chunks", "structure")
