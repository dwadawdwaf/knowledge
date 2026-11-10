"""放宽模型标识字段长度。

真实部署的模型名会带厂商前缀、量化方式与版本后缀，例如
``HiModel_xh2_qwen3.6-35b-a3b_w4a8_256_256k_b1_1chip_2cores_v1.4.0_20260716``
共 73 字符。原 ``messages.model_revision`` 上限 64，问答生成成功后写入消息会抛
``StringDataRightTruncation``，表现为答案已生成但接口 500。
索引构建的 ``embedding_model`` 同样属于外部标识，一并放宽到 200。
"""

import sqlalchemy as sa
from alembic import op

revision = "0004_model_identifier_length"
down_revision = "0003_chunk_warnings"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "messages",
        "model_revision",
        existing_type=sa.String(length=64),
        type_=sa.String(length=200),
        existing_nullable=False,
    )
    op.alter_column(
        "index_builds",
        "embedding_model",
        existing_type=sa.String(length=120),
        type_=sa.String(length=200),
        existing_nullable=False,
    )


def downgrade() -> None:
    # PostgreSQL 会拒绝缩短含超长标识的列，不截断历史模型名。
    op.alter_column(
        "index_builds",
        "embedding_model",
        existing_type=sa.String(length=200),
        type_=sa.String(length=120),
        existing_nullable=False,
    )
    op.alter_column(
        "messages",
        "model_revision",
        existing_type=sa.String(length=200),
        type_=sa.String(length=64),
        existing_nullable=False,
    )
