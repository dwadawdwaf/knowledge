"""会话归档状态。

与知识库归档保持一致：归档会话保留消息与引用，只是不进历史列表；
把 status 改回 active 即可恢复，不删除任何消息。
"""

import sqlalchemy as sa
from alembic import op

revision = "0006_conversation_archived"
down_revision = "0005_kb_archived_status"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "conversations",
        sa.Column("status", sa.String(length=16), nullable=False, server_default="active"),
    )
    op.create_check_constraint(
        "ck_conversation_status",
        "conversations",
        "status IN ('active', 'archived')",
    )


def downgrade() -> None:
    op.drop_constraint("ck_conversation_status", "conversations", type_="check")
    op.drop_column("conversations", "status")
