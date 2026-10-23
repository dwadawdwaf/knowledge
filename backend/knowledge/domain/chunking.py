"""分段：纯函数，无 I/O。

按块内段落切分，超长段落按字符窗口滑动并保留重叠；每段携带回原文的 locator。
"""

from __future__ import annotations

from dataclasses import dataclass

from knowledge.domain.entities import Locator, ParsedBlock


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    text: str
    locator: dict


def _split_text(text: str, max_chars: int, overlap: int) -> list[tuple[int, int]]:
    """返回 [(start, end)]，按段落边界优先，超长段落窗口滑动。"""
    spans: list[tuple[int, int]] = []
    pos = 0
    length = len(text)

    while pos < length:
        end = min(pos + max_chars, length)
        if end < length:
            window = text[pos:end]
            cut = max(window.rfind("\n\n"), window.rfind("\n"), window.rfind("。"))
            if cut > max_chars // 3:
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
            )
            chunks.append(TextChunk(ordinal=ordinal, text=piece, locator=locator.to_dict()))
            ordinal += 1

    return chunks
