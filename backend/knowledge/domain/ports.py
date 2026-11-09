"""端口定义：外部依赖的能力边界，实现放在 adapters/。"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from typing import Protocol
from uuid import UUID

from knowledge.domain.entities import Evidence, GeneratedAnswer, ParsedBlock


class Parser(Protocol):
    """把原文件字节转成有序内容块。"""

    name: str

    def supports(self, media_type: str, filename: str) -> bool: ...

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]: ...


class Embedder(Protocol):
    dimension: int
    model: str

    def embed(self, texts: Sequence[str]) -> list[list[float]]: ...


class Reranker(Protocol):
    """返回与候选顺序一致的相关性分数；分数不表示事实支持概率。"""

    def rank(self, query: str, texts: Sequence[str]) -> list[float]: ...


class KeywordIndex(Protocol):
    """仅保存检索文本的倒排索引，业务权限和内容仍以 PostgreSQL 为准。"""

    def missing_builds(self, builds: Sequence[UUID]) -> list[UUID]: ...

    def index_builds(
        self, builds: Mapping[UUID, UUID], chunks: Iterable[tuple[UUID, UUID, str]],
    ) -> None: ...

    def search(self, query: str, builds: Sequence[UUID], limit: int) -> list[tuple[UUID, float]]: ...

    def delete_document(self, document_id: UUID) -> None: ...


class Generator(Protocol):
    revision: str

    def generate(self, question: str, evidence: Sequence[Evidence]) -> GeneratedAnswer: ...


class ObjectStorage(Protocol):
    def put(self, object_key: str, data: bytes) -> None: ...

    def get(self, object_key: str) -> bytes: ...

    def delete(self, object_key: str) -> None: ...

    def exists(self, object_key: str) -> bool: ...

    def iter_objects(self) -> Sequence[tuple[str, float]]:
        """列出全部对象及已存在的秒数，供孤儿回收扫描使用。"""
        ...
