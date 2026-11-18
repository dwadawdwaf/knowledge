"""配置：全部经环境变量注入，前缀 KS_。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict

from knowledge.domain.entities import ParserProfile


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
    document_parser_profile: ParserProfile = "legacy-v1"
    mineru_url: str = "http://127.0.0.1:8003"
    mineru_token: str | None = None
    mineru_timeout_seconds: float = 330.0
    # MinerU 结果 ZIP 体积上限（含未读取的图片）；JSON 正文仍受 parse_output_bytes 限制。
    mineru_output_bytes: int = 64 * 1024 * 1024
    query_max_chars: int = 2000
    top_k_default: int = 5
    event_stream_limit: int = Field(default=8, ge=1, le=16)
    top_k_max: int = 20
    kb_scope_max: int = 20

    retrieval_profile: Literal["ragflow-v1"] = "ragflow-v1"
    search_engine: Literal["postgresql", "opensearch"] = "postgresql"
    opensearch_url: str = "http://127.0.0.1:19276"
    opensearch_index: str = "ks-ragflow-native-v2"
    opensearch_timeout_seconds: float = Field(default=30, gt=0)
    evidence_candidate_limit: int = Field(default=20, ge=1, le=100)
    evidence_max_chars: int = Field(default=6000, ge=800, le=32000)
    evidence_max_items: int = 6
    evidence_min_score: float = 0.0

    # 会话上下文：只取最近 N 条历史消息进入模型，不做摘要与 token 预算。
    context_recent_messages: int = Field(default=12, ge=0, le=50)
    # 上下文感知检索时，从上文带出的字符上限；0 表示关闭问题重写。
    context_rewrite_max_chars: int = Field(default=400, ge=0, le=2000)

    lease_seconds: int = 120
    heartbeat_seconds: int = 30
    max_attempts: int = 3
    worker_poll_seconds: float = 1.0
    # 孤儿对象回收阈值：对象写入成功但事务失败时，超过该时长且无引用即回收
    orphan_max_age_seconds: int = 3600




@lru_cache
def get_settings() -> Settings:
    return Settings()
