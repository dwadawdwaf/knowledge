"""配置：全部经环境变量注入，前缀 KS_。"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal
from urllib.parse import urlsplit

from pydantic import Field, model_validator
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
    pdf_parser_profile: ParserProfile = "mineru-api4.1-v1"
    mineru_url: str = "http://127.0.0.1:8003"
    mineru_token: str | None = None
    mineru_timeout_seconds: float = 330.0
    # MinerU 结果 ZIP 体积上限（含未读取的图片）；JSON 正文仍受 parse_output_bytes 限制。
    mineru_output_bytes: int = 64 * 1024 * 1024
    query_max_chars: int = 2000
    top_k_default: int = 5
    top_k_max: int = 20
    kb_scope_max: int = 20

    recall_limit: int = 50
    rrf_k: int = 60
    retrieval_profile: Literal["baseline-v1", "optimized-v2", "fts-v1", "bm25-v1"] = "baseline-v1"
    opensearch_url: str | None = None
    opensearch_index: str = Field(default="ks-chunks-bm25-v1", pattern=r"^ks-[a-z0-9][a-z0-9-]{0,62}$")
    opensearch_timeout_seconds: float = Field(default=10, gt=0, le=60)
    evidence_coverage: bool = False
    structured_context: bool = False
    keyword_weight: float = Field(default=0.5, ge=0, le=2)
    # 局部匹配与全文相似度尺度不同，独立设门槛，不能共用0.06。
    keyword_local_floor: float = Field(default=0.3, ge=0, le=1)
    semantic_weight: float = Field(default=1.0, gt=0, le=2)
    query_variants_max: int = Field(default=3, ge=1, le=4)
    evidence_candidate_limit: int = Field(default=20, ge=1, le=100)
    evidence_max_chars: int = Field(default=6000, ge=800, le=32000)
    evidence_neighbor_limit: int = Field(default=2, ge=0, le=6)
    reranker_url: str | None = None
    reranker_model: str = ""
    reranker_timeout_seconds: float = Field(default=20, gt=0, le=120)
    # 关键词候选下限，不能当成答案支持度或实际语料的效果承诺。
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
    chunk_strategy: Literal["legacy-v1", "structure-v2"] = "legacy-v1"
    pdf_chunk_strategy: Literal["pdf-layout-v1"] | None = None

    @model_validator(mode="after")
    def validate_chunking(self) -> Settings:
        if self.chunk_max_chars < 16 or not 0 <= self.chunk_overlap_chars < self.chunk_max_chars:
            raise ValueError("分块长度须至少16，重叠须非负且小于分块长度")
        return self

    @model_validator(mode="after")
    def validate_opensearch(self) -> Settings:
        if self.retrieval_profile == "bm25-v1" and not self.opensearch_url:
            raise ValueError("bm25-v1 需要配置本机 KS_OPENSEARCH_URL")
        if self.opensearch_url:
            parsed = urlsplit(self.opensearch_url)
            if (parsed.scheme not in {"http", "https"} or parsed.hostname not in {"127.0.0.1", "::1"}
                    or parsed.username or parsed.password or parsed.path not in {"", "/"}
                    or parsed.query or parsed.fragment):
                raise ValueError("OpenSearch 仅允许本机回环地址")
            _ = parsed.port
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()
