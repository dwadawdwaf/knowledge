"""向量化适配器。未配置外部端点时使用确定性的本地实现，便于无模型环境跑通链路。"""

from __future__ import annotations

import hashlib
import json
import math
import socket
import urllib.error
import urllib.request
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from knowledge.adapters import outbound
from knowledge.config import Settings
from knowledge.domain.errors import ModelTimeout, ModelTransientUnavailable, ModelUnavailable


class _EmbeddingItem(BaseModel):
    model_config = ConfigDict(strict=True, allow_inf_nan=False)

    index: int | None = Field(default=None, ge=0)
    embedding: list[float]


class _EmbeddingResponse(BaseModel):
    data: list[_EmbeddingItem] = Field(min_length=1)


class HashEmbedder:
    """基于字符二元组的哈希向量：确定性、无外部依赖，仅用于开发与测试。"""

    def __init__(self, dimension: int = 1024, model: str = "hash-1024") -> None:
        self.dimension = dimension
        self.model = model

    def _tokens(self, text: str) -> list[str]:
        chars = [char for char in text.lower() if not char.isspace()]
        grams = [chars[i] + chars[i + 1] for i in range(len(chars) - 1)]
        return grams or chars or ["<empty>"]

    def _one(self, text: str) -> list[float]:
        vector = [0.0] * self.dimension
        for token in self._tokens(text):
            digest = hashlib.blake2b(token.encode("utf-8"), digest_size=8).digest()
            bucket = int.from_bytes(digest[:4], "big") % self.dimension
            sign = 1.0 if digest[4] % 2 == 0 else -1.0
            vector[bucket] += sign
        norm = math.sqrt(sum(value * value for value in vector))
        if norm == 0:
            return vector
        return [value / norm for value in vector]

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        return [self._one(text) for text in texts]


class HttpEmbedder:
    """OpenAI 兼容的 /embeddings 端点。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        dimension: int,
        batch: int = 16,
        timeout: float = 30.0,
        api_key: str | None = None,
        dimensions: int | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dimension = dimension
        self.batch = max(1, batch)
        self.timeout = timeout
        self.api_key = api_key
        self.dimensions = dimensions

    def _post(self, texts: Sequence[str]) -> list[list[float]]:
        payload: dict[str, object] = {"model": self.model, "input": list(texts)}
        if self.dimensions is not None:
            payload["dimensions"] = self.dimensions
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/embeddings",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        try:
            with outbound.open_request(request, timeout=self.timeout) as response:
                body = _EmbeddingResponse.model_validate_json(response.read())
        except TimeoutError as exc:  # pragma: no cover - 依赖外部服务
            raise ModelTimeout("向量服务超时") from exc
        except urllib.error.HTTPError as exc:  # pragma: no cover
            if exc.code == 429 or 500 <= exc.code < 600:
                raise ModelTransientUnavailable(f"向量服务返回 {exc.code}") from exc
            raise ModelUnavailable(f"向量服务返回 {exc.code}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover
            if isinstance(exc.reason, TimeoutError):
                raise ModelTimeout("向量服务超时") from exc
            if isinstance(exc.reason, ConnectionRefusedError):
                raise ModelTransientUnavailable("向量服务连接被拒绝") from exc
            if isinstance(exc.reason, socket.gaierror):
                raise ModelTransientUnavailable("向量服务名称解析失败") from exc
            raise ModelTransientUnavailable("向量服务不可用") from exc
        except ValidationError as exc:
            raise ModelUnavailable("向量服务响应结构无效") from exc

        data = body.data
        if len(data) != len(texts):
            raise ModelUnavailable("向量服务返回数量不符")
        if any(item.index is not None for item in data):
            if sorted(item.index for item in data if item.index is not None) != list(
                range(len(texts))
            ):
                raise ModelUnavailable("向量服务返回索引无效")
            data.sort(key=lambda item: item.index if item.index is not None else -1)
        vectors = [item.embedding for item in data]
        for vector in vectors:
            if len(vector) != self.dimension:
                raise ModelUnavailable(f"向量维度不符：期望 {self.dimension}，实际 {len(vector)}")
        return vectors

    def embed(self, texts: Sequence[str]) -> list[list[float]]:
        result: list[list[float]] = []
        for start in range(0, len(texts), self.batch):
            result.extend(self._post(texts[start : start + self.batch]))
        return result


def build_embedder(settings: Settings) -> HashEmbedder | HttpEmbedder:
    if settings.embedding_base_url:
        return HttpEmbedder(
            base_url=settings.embedding_base_url,
            model=settings.embedding_model,
            dimension=settings.embedding_dim,
            batch=settings.embedding_batch,
            api_key=settings.embedding_api_key,
            dimensions=settings.embedding_dimensions,
        )
    return HashEmbedder(dimension=settings.embedding_dim)
