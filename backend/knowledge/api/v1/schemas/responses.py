"""出参模型。与领域实体分离，内部字段不进入响应。"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field


class KnowledgeBaseOut(BaseModel):
    id: uuid.UUID
    name: str
    status: str
    created_at: str


class JobOut(BaseModel):
    id: uuid.UUID
    kind: str
    state: str
    stage: str | None = None
    attempt: int = 0
    max_attempts: int = 3
    last_error: dict | None = None


class DocumentOut(BaseModel):
    id: uuid.UUID
    kb_id: uuid.UUID
    title: str
    status: str
    generation: int
    current_version_id: uuid.UUID | None = None
    active_build_id: uuid.UUID | None = None
    pending_build_id: uuid.UUID | None = None
    version_no: int | None = None
    pending_build_state: str | None = None
    parser_profile: str | None = None
    job: JobOut | None = None


class UploadAck(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    build_id: uuid.UUID
    job_id: uuid.UUID
    deduplicated: bool = False
    parser_profile: str = "legacy-v1"


class ReindexAck(BaseModel):
    document_id: uuid.UUID
    version_id: uuid.UUID
    build_id: uuid.UUID
    job_id: uuid.UUID
    generation: int


class SearchHitOut(BaseModel):
    # 检索服务返回的即字符串 ID，JSON 输出与 UUID 形式一致
    document_id: str
    kb_id: str
    kb_name: str
    version_id: str
    build_id: str
    chunk_id: str
    title: str
    excerpt: str
    locator: dict
    score: float
    score_type: str


class SearchResultOut(BaseModel):
    mode: str
    hits: list[SearchHitOut]
    score_type: str = "rrf"
    degraded: bool = False
    warnings: list[str] = Field(default_factory=list)


class CitationOut(BaseModel):
    index: int
    document_id: uuid.UUID
    version_id: uuid.UUID
    build_id: uuid.UUID
    chunk_id: uuid.UUID
    title: str
    excerpt: str
    locator: dict


class AnswerOut(BaseModel):
    answer_status: str
    answer: str
    citations: list[CitationOut] = Field(default_factory=list)
    retrieval: dict = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)


class ChunkOut(BaseModel):
    chunk_id: uuid.UUID
    document_id: uuid.UUID
    version_id: uuid.UUID
    build_id: uuid.UUID
    title: str
    text: str
    locator: dict
    warnings: list[str] = Field(default_factory=list)


class ConversationOut(BaseModel):
    id: uuid.UUID
    title: str
