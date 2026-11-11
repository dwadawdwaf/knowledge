"""从解析块识别显式标题与表格行，并保留原文偏移。"""

from __future__ import annotations

import re
from dataclasses import replace
from itertools import pairwise

from knowledge.domain.entities import ParsedBlock
from knowledge.domain.pdf_layout import PDF_INDEPENDENT

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_CHAPTER = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+[章节编部]\s*\S.*$")
_ARTICLE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+条")


def _elements(block: ParsedBlock, *, pdf_layout: bool = False) -> list[ParsedBlock]:
    """识别行首显式结构，偏移仍属于原始解析块。"""
    if block.locator.type == "pptx" or block.kind in {"table", "table_row", "figure", "caption", "footnote", "header", "footer", "code"} or (
        pdf_layout and block.kind in PDF_INDEPENDENT
    ):
        return [block]
    lines = list(re.finditer(r"[^\n]+(?:\n|$)", block.text))
    boundaries = {0, len(block.text)}
    if pdf_layout and block.kind == "text":
        boundaries.update(match.end() for match in re.finditer(r"\r?\n[ \t]*\r?\n", block.text))
    kinds: dict[int, tuple[str, int | None]] = {}
    for line in lines:
        value = line.group().strip()
        match = _HEADING.match(value)
        if match or _CHAPTER.match(value):
            boundaries.update((line.start(), line.end()))
            kinds[line.start()] = ("heading", len(match[1]) if match else 1)
        elif _ARTICLE.match(value):
            boundaries.update((line.start(), line.end()))
            kinds[line.start()] = ("article", None)
    points = sorted(boundaries)
    return [
        replace(
            block,
            text=block.text[start:end],
            locator=replace(
                block.locator,
                char_start=block.locator.char_start + start,
                char_end=block.locator.char_start + end,
            ),
            kind=kinds.get(start, (block.kind, block.heading_level))[0],
            heading_level=kinds.get(start, (block.kind, block.heading_level))[1],
        )
        for start, end in pairwise(points)
        if block.text[start:end].strip()
    ]


def _table_rows(block: ParsedBlock) -> list[ParsedBlock]:
    if block.kind != "table" or (
        block.locator.type not in {"csv", "xlsx"} and "\t" not in block.text
    ):
        return [block]
    rows = list(re.finditer(r"[^\n]+", block.text))
    result = []
    numbers = block.metadata.get("row_numbers", [])
    data_index = 0
    for row in rows:
        if row.start() == 0 and block.locator.type in {"csv", "xlsx"}:
            continue
        locator = replace(
            block.locator,
            char_start=block.locator.char_start + row.start(),
            char_end=block.locator.char_start + row.end(),
        )
        if data_index < len(numbers):
            locator = replace(locator, row_start=numbers[data_index], row_end=numbers[data_index])
        result.append(replace(block, text=row.group(), locator=locator, kind="table_row"))
        data_index += 1
    return result or [block]
