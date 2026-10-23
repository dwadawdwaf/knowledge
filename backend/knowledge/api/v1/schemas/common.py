"""统一响应外壳。所有接口只返回这两种结构。"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class Meta(BaseModel):
    request_id: str = Field(default="", description="请求追踪标识")


class Envelope(BaseModel):
    data: Any
    meta: Meta


class ErrorDetail(BaseModel):
    code: str
    message: str
    retryable: bool = False


class ErrorEnvelope(BaseModel):
    error: ErrorDetail
    meta: Meta


def envelope(data: Any, request_id: str) -> dict[str, Any]:
    return {"data": data, "meta": {"request_id": request_id}}
