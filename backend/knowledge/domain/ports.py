"""端口定义：外部依赖的能力边界，实现放在 adapters/。"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Protocol

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
