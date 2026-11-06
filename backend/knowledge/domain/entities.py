"""领域实体与值对象。仅用标准库。"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Any, Literal

ParserProfile = Literal["legacy-v1", "mineru-api4.1-v1"]


class Lifecycle(str, enum.Enum):
    ACTIVE = "active"
    DELETING = "deleting"
    DELETED = "deleted"


class BuildState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    READY = "ready"
    FAILED = "failed"
    CANCELLED = "cancelled"
    RETIRED = "retired"


class JobState(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"


class JobKind(str, enum.Enum):
    INGEST = "ingest"
    REINDEX = "reindex"
    CLEANUP = "cleanup"


class AnswerStatus(str, enum.Enum):
    ANSWERED = "answered"
    NO_EVIDENCE = "no_evidence"
    CONFLICTING = "conflicting_evidence"


@dataclass(frozen=True)
class Actor:
    """一次调用的授权上下文。owner_id 只能源于此。"""

    subject_id: str
    tenant_id: str
    token_id: str | None = None


@dataclass(frozen=True)
class Locator:
    """分块在原文中的位置。页码从 1 开始，字符偏移按码点、左闭右开。"""

    type: str
    char_start: int
    char_end: int
    precision: str = "char"
    page: int | None = None
    block: int | None = None
    paragraph: int | None = None
    # 表格类文件（xlsx）用工作表名与行区间定位，行号从 1 开始、含表头。
    sheet: str | None = None
    row_start: int | None = None
    row_end: int | None = None
    # 解析器原始块位置：[页码, 左, 右, 上, 下]；不代表子片段精确字框。
    positions: tuple[tuple[int, float, float, float, float], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "type": self.type,
            "char_start": self.char_start,
            "char_end": self.char_end,
            "precision": self.precision,
        }
        if self.page is not None:
            out["page"] = self.page
        if self.block is not None:
            out["block"] = self.block
        if self.paragraph is not None:
            out["paragraph"] = self.paragraph
        if self.sheet is not None:
            out["sheet"] = self.sheet
        if self.row_start is not None:
            out["row_start"] = self.row_start
        if self.row_end is not None:
            out["row_end"] = self.row_end
        if self.positions:
            out["positions"] = [list(position) for position in self.positions]
        return out


@dataclass(frozen=True)
class ParsedBlock:
    """解析器输出的一个有序内容块。"""

    text: str
    locator: Locator
    warnings: tuple[str, ...] = ()
    # 解析器确实提供的结构；未识别时为空，不根据文件类型伪造标题或坐标。
    kind: str = "text"
    heading_level: int | None = None
    table_id: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class Evidence:
    """本轮送入模型的一段证据。"""

    key: str
    chunk_id: str
    document_id: str
    version_id: str
    build_id: str
    title: str
    text: str
    locator: dict[str, Any]


@dataclass(frozen=True)
class GeneratedSection:
    text: str
    evidence_keys: tuple[str, ...] = ()


@dataclass(frozen=True)
class GeneratedAnswer:
    sections: tuple[GeneratedSection, ...]
    model_revision: str = ""


@dataclass(frozen=True)
class Citation:
    index: int
    document_id: str
    version_id: str
    build_id: str
    chunk_id: str
    title: str
    excerpt: str
    locator: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "document_id": self.document_id,
            "version_id": self.version_id,
            "build_id": self.build_id,
            "chunk_id": self.chunk_id,
            "title": self.title,
            "excerpt": self.excerpt,
            "locator": self.locator,
        }


@dataclass(frozen=True)
class SearchHit:
    document_id: str
    kb_id: str
    kb_name: str
    version_id: str
    build_id: str
    chunk_id: str
    title: str
    text: str
    locator: dict[str, Any]
    score: float
    score_type: str = "rrf"
    warnings: tuple[str, ...] = ()
    structure: dict[str, Any] = field(default_factory=dict)
    diagnostics: dict[str, Any] = field(default_factory=dict)

    @property
    def excerpt(self) -> str:
        return self.text.strip()


@dataclass
class Answer:
    status: str
    text: str
    citations: list[Citation] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    retrieval_mode: str = "hybrid"
    hits: int = 0
    degraded: bool = False
