"""分段：纯函数，无 I/O。

按块内段落切分，超长段落按字符窗口滑动并保留重叠；每段携带回原文的 locator。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.pdf_layout import PDF_LAYOUT_STRATEGY


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
    *,
    strategy: str = "legacy-v1",
    title: str = "",
) -> list[TextChunk]:
    if max_chars < 16 or overlap < 0 or overlap >= max_chars:
        raise ValueError("分块长度须至少16字符，重叠须非负且小于分块长度")
    if strategy in {"structure-v2", PDF_LAYOUT_STRATEGY}:
        from knowledge.domain.structure import chunk_structured

        return chunk_structured(blocks, max_chars, overlap, title, pdf_layout=strategy == PDF_LAYOUT_STRATEGY)
    if strategy != "legacy-v1":
        raise ValueError("未知分块策略")
    chunks: list[TextChunk] = []
    ordinal = 0

    for block in blocks:
        text = block.text
        if not text.strip():
            continue

        for start, end in _split_text(text, max_chars, overlap):
            piece = text[start:end]
            if not piece.strip():
                continue
            base = block.locator
            locator = Locator(
                type=base.type,
                char_start=base.char_start + start,
                char_end=base.char_start + end,
                precision=base.precision,
                page=base.page,
                block=base.block,
                paragraph=base.paragraph,
                sheet=base.sheet,
                row_start=base.row_start,
                row_end=base.row_end,
                positions=base.positions,
            )
            chunks.append(
                TextChunk(
                    ordinal=ordinal,
                    text=piece,
                    locator=locator.to_dict(),
                    warnings=block.warnings,
                )
            )
            ordinal += 1

    return chunks
