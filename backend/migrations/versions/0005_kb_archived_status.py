"""知识库归档状态。

归档只改变可见性：数据、文档与分块保留，列表不显示，检索与上传范围也拒绝；
需要恢复时把 status 改回 active 即可，不经过删除流程。
"""

import sqlalchemy as sa
from alembic import op

revision = "0005_kb_archived_status"
down_revision = "0004_model_identifier_length"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_constraint("ck_kb_status", "knowledge_bases", type_="check")
    op.create_check_constraint(
        "ck_kb_status",
        "knowledge_bases",
        "status IN ('active', 'deleting', 'deleted', 'archived')",
    )


def downgrade() -> None:
    # 回退前必须先把归档库恢复为 active，否则新约束会拒绝旧数据。
    bind = op.get_bind()
    remaining = bind.execute(
        sa.text("SELECT count(*) FROM knowledge_bases WHERE status = 'archived'")
    ).scalar_one()
    if remaining:
        raise RuntimeError("仍有归档知识库，请先恢复为 active 再回退迁移")
    op.drop_constraint("ck_kb_status", "knowledge_bases", type_="check")
    op.create_check_constraint(
        "ck_kb_status",
        "knowledge_bases",
        "status IN ('active', 'deleting', 'deleted')",
    )
