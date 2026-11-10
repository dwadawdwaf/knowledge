"""汇合归档与检索优化迁移链，不重复执行任何 DDL。"""

revision = "0007_merge_optimization"
down_revision = ("0006_conversation_archived", "0005_chunk_fts")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
