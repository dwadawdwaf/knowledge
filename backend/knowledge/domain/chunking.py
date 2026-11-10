"""分段：纯函数，无 I/O。

按块内段落切分，超长段落按字符窗口滑动并保留重叠；每段携带回原文的 locator。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from knowledge.domain.entities import ParsedBlock

EVIDENCE_UNIT_STRATEGY = "evidence-unit-v1"


def upgrade_chunk_config(source: dict, media_type: str) -> dict:
    """新构建统一使用现行策略；不改历史配置及原有长度预算。"""
    config = {**source, "strategy": EVIDENCE_UNIT_STRATEGY}
    profile = config.get("parser_profile", "legacy-v1")
    if media_type in {"text/plain", "text/markdown"} and isinstance(profile, str) and profile.startswith("deepdoc-"):
        config.update(parser_profile="legacy-v1", parser_profile_migrated=True)
    return config


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    text: str
    locator: dict
    warnings: tuple[str, ...] = ()
    retrieval_text: str = ""
    structure: dict[str, Any] = field(default_factory=dict)


def _split_text(
    text: str, max_chars: int, overlap: int, *, sentence_boundaries: bool = False,
    table_rows: bool = False,
) -> list[tuple[int, int]]:
    """返回 [(start, end)]，按段落边界优先，超长段落窗口滑动。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    length = len(text)

    while pos < length:
        end = min(pos + max_chars, length)
        if end < length:
            window = text[pos:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"))
            if sentence_boundaries:
                sentence_ends = list(re.finditer(r"[.!?。！？](?=\s|[\u3400-\u9fff])", window))
                if sentence_ends and sentence_ends[-1].start() > max_chars // 3:
                    cut = sentence_ends[-1].start()
                elif cut <= max_chars // 3:
                    cut = max(cut, window.rfind(" "))
            if table_rows:
                row_end = window.rfind("\n")
                if row_end >= 0:
                    end = pos + row_end + 1
            elif cut > max_chars // 3:
                end = pos + cut + 1
        spans.append((pos, end))
        if end >= length:
            break
        pos = max(end - overlap, pos + 1)

    return spans


def chunk_blocks(
    blocks: list[ParsedBlock],
    max_chars: int = 800,
    overlap: int = 100,
) -> list[TextChunk]:
    if max_chars < 16 or overlap < 0 or overlap >= max_chars:
        raise ValueError("分块长度须至少16字符，重叠须非负且小于分块长度")
    from knowledge.domain.structure import chunk_evidence_units

    return chunk_evidence_units(blocks, max_chars, overlap)
