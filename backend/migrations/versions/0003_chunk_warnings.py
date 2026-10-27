"""Store parse-degradation warnings per chunk.

build 级 warnings 只是所有块的并集，无法判断某一块是 OCR 还是被线性化的表格；
按块保存后，前端与检索才能只对受影响的片段提示。
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects.postgresql import JSONB

revision = "0003_chunk_warnings"
down_revision = "0002_job_target"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chunks",
        sa.Column("warnings", JSONB(), nullable=False, server_default=sa.text("'[]'::jsonb")),
    )


def downgrade() -> None:
    op.drop_column("chunks", "warnings")
