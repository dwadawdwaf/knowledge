"""配置：全部经环境变量注入，前缀 KS_。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="KS_", env_file=".env", extra="ignore")

    database_url: str = "postgresql+psycopg://postgres:postgres@localhost:5432/knowledge"
    # 浏览器验收使用独立库：它用桩模型（hash-1024），与真实模型不能共用同一个向量索引。
    smoke_database_url: str | None = None
    # 数据库在远端时，由启动器按需建立本机到服务器的 SSH 隧道（仅开发用）。
    tunnel_ssh: str | None = None
    tunnel_key: str | None = None
    storage_root: str = "./.data/objects"

    token_secret: str = "dev-only-secret"
    token_ttl_seconds: int = 24 * 3600

    embedding_base_url: str | None = None
    embedding_model: str = "hash-1024"
    embedding_dim: int = 1024
    embedding_batch: int = 16
    embedding_api_key: str | None = None
    # 请求端点下发的 dimensions 参数（部分服务要求显式指定，如 embedding-3 系列）
    embedding_dimensions: int | None = None

    generator_base_url: str | None = None
    generator_model: str = "stub"
    generator_api_key: str | None = None
    generator_timeout_seconds: float = 60.0

    max_upload_bytes: int = 50 * 1024 * 1024
    parse_timeout_seconds: float = 60.0
    parse_cpu_seconds: float = 30.0
    parse_memory_bytes: int = 512 * 1024 * 1024
    parse_output_bytes: int = 16 * 1024 * 1024
    docx_max_entries: int = 2048
    docx_max_uncompressed_bytes: int = 100 * 1024 * 1024
    # 表格：单个文件最多读取的行数（超出显式告警，不静默截断），以及每块行数。
    xlsx_max_rows: int = 5000
    xlsx_rows_per_block: int = 40
    document_parser_profile: Literal["legacy-v1", "deepdoc-v0.22.0-v1"] = "legacy-v1"
    deepdoc_url: str = "http://127.0.0.1:9381"
    deepdoc_token: str | None = None
    deepdoc_timeout_seconds: float = 330.0
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
