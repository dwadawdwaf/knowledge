"""配置：全部经环境变量注入，前缀 KS_。"""

from __future__ import annotations

from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/knowledge"
    storage_root: str = "./.data/objects"

    token_secret: str = "dev-only-secret"
    token_ttl_seconds: int = 24 * 3600

    embedding_base_url: str | None = None
    embedding_model: str = "hash-1024"
    embedding_dim: int = 1024
    embedding_batch: int = 16

    generator_base_url: str | None = None
    generator_model: str = "stub"
    generator_timeout_seconds: float = 60.0

    max_upload_bytes: int = 50 * 1024 * 1024
    query_max_chars: int = 2000
    top_k_default: int = 5
    top_k_max: int = 20
    kb_scope_max: int = 20

    recall_limit: int = 50
    rrf_k: int = 60
    # 关键词相似度下限：低于视为不相关。0.06 按自制评测语料（含 5 部抓取法律法规、
    # ~490 分块）标定：语料外问题关键词路最高 0.059，真实命中最低 0.058——两者在
    # 关键词路接近重叠，靠语义路兜底（见 tests/integration/test_effectiveness.py）；
    # 换向量模型或扩充语料后必须重新标定。
    similarity_floor: float = 0.06
    # 余弦距离上限：超过视为不相关。默认值按本地哈希向量标定，换真实模型后必须重标。
    semantic_max_distance: float = 0.85
    evidence_max_items: int = 6
    evidence_min_score: float = 0.0

    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    max_attempts: int = 3
    worker_poll_seconds: float = 1.0
    # 孤儿对象回收阈值：对象写入成功但事务失败时，超过该时长且无引用即回收
    orphan_max_age_seconds: int = 3600

    chunk_max_chars: int = 800
    chunk_overlap_chars: int = 100


@lru_cache
def get_settings() -> Settings:
    return Settings()
