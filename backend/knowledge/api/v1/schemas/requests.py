"""入参模型。只描述传输层，不复用领域实体。"""

from __future__ import annotations

import uuid

from pydantic import BaseModel, Field, field_validator


class CreateKnowledgeBaseRequest(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=1000)


class CreateConversationRequest(BaseModel):
    title: str = Field(default="", max_length=200)


class SearchRequest(BaseModel):
    query: str
    knowledge_base_ids: list[uuid.UUID]
    document_ids: list[uuid.UUID] = Field(default_factory=list)
    mode: str = "hybrid"
    top_k: int | None = Field(default=None, ge=1, le=20)

    @field_validator("mode")
    @classmethod
    def _mode(cls, value: str) -> str:
        if value not in {"hybrid", "keyword", "semantic"}:
            raise ValueError("mode 必须是 hybrid / keyword / semantic")
        return value


class AskRequest(BaseModel):
    question: str
    knowledge_base_ids: list[uuid.UUID]
    document_ids: list[uuid.UUID] = Field(default_factory=list)
    top_k: int | None = Field(default=None, ge=1, le=20)
    conversation_id: uuid.UUID | None = None
    stream: bool = False
