"""按文档元素组织片段，保存合并前的每个原文区间；无 I/O。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from dataclasses import replace
from itertools import pairwise
from typing import Any

from knowledge.domain.chunking import EVIDENCE_UNIT_STRATEGY, TextChunk, _split_text
from knowledge.domain.entities import ParsedBlock
from knowledge.domain.pdf_layout import (
    PDF_INDEPENDENT,
    order_pdf_blocks,
    pdf_continuation,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_CHAPTER = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+[章节编部]\s*\S.*$")
_ARTICLE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+条")
_SUBPOINT = re.compile(r"^\s*(?:[（(][一二三四五六七八九十百\d]+[）)]|[一二三四五六七八九十百\d]+[、.．])")
_LIST_ITEM = re.compile(r"^(?:\\?[-+*•▪◦]\s+|\d+(?:\.\d+)*\\?[.)、]\s+)")
_SENTENCE_BOUNDARY = re.compile(r"[。！？；]+|[.!?;]+(?=\s|$)|\r?\n")


def _elements(block: ParsedBlock) -> list[ParsedBlock]:
    """识别行首显式结构，偏移仍属于原始解析块。"""
    if block.kind in PDF_INDEPENDENT or block.kind in {"header", "footer"}:
        return [block]
    lines = list(re.finditer(r"[^\n]+(?:\n|$)", block.text))
    boundaries = {0, len(block.text)}
    boundaries.update(match.end() for match in re.finditer(r"\r?\n[ \t]*\r?\n", block.text))
    kinds: dict[int, tuple[str, int | None]] = {}
    default = ("text", None) if block.kind == "heading" else (block.kind, block.heading_level)
    for line in lines:
        value = line.group().strip()
        match = _HEADING.match(value)
        label = (match[2] if match else value).strip("*_` ")
        if _ARTICLE.match(label):
            boundaries.add(line.start())
            kinds[line.start()] = ("article", None)
        elif _SUBPOINT.match(label) or _LIST_ITEM.match(value):
            boundaries.add(line.start())
            kinds[line.start()] = ("text", None)
        elif match or _CHAPTER.match(label) or (
            block.kind == "heading" and line is lines[0]
        ):
            boundaries.update((line.start(), line.end()))
            default_level = block.heading_level or 1
            kinds[line.start()] = ("heading", len(match[1]) if match else default_level)
    points = sorted(boundaries)
    points = [0, *(start for start, end in pairwise(points) if start and block.text[start:end].strip()), len(block.text)]
    return [
        replace(
            block,
            text=block.text[start:end],
            locator=replace(
                block.locator,
                char_start=block.locator.char_start + start,
                char_end=block.locator.char_start + end,
            ),
            kind=kinds.get(start, default)[0],
            heading_level=kinds.get(start, default)[1],
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


def _compatible(previous: ParsedBlock, current: ParsedBlock) -> bool:
    a, b = previous.locator, current.locator
    if a.type != b.type or a.sheet != b.sheet:
        return False
    if previous.kind in {"table", "table_row"} or current.kind in {"table", "table_row"}:
        return False
    if current.kind in {"heading", "article", "figure", "caption", "footnote"}:
        return False
    if a.type == "pptx" and a.page != b.page:
        return False
    if a.page != b.page:
        # 跨页只关联相邻页中同一章节的明确续句；没有章节线索时保持独立。
        return (
            a.page is not None
            and b.page == a.page + 1
            and not previous.text.rstrip().endswith(("。", "！", "？", ".", "!", "?", "：", ":"))
        )
    return True


def _separator(previous: ParsedBlock, current: ParsedBlock) -> str:
    if previous.text.endswith("\n") or current.text.startswith(("\n", "\r\n")):
        return ""
    if replace(previous.locator, char_start=0, char_end=0) == replace(
        current.locator, char_start=0, char_end=0,
    ) and previous.locator.char_end == current.locator.char_start:
        return ""
    return "\n"


def _table_context(
    element: ParsedBlock, headers: dict[str, tuple[str, dict[str, Any]]],
) -> ParsedBlock:
    if element.kind != "table_row" or not element.table_id:
        return element
    cells = re.sub(r"^表格\d+ 第\d+行：", "", element.text).split(
        "\t" if "\t" in element.text else "｜"
    )
    row = element.metadata.get("row", element.locator.row_start)
    if row == 1 and len(cells) > 1 and all(
        cell.strip() and not re.fullmatch(r"[\d.,%￥$+-]+", cell.strip()) for cell in cells
    ):
        headers[element.table_id] = (element.text, element.locator.to_dict())
        element = replace(element, metadata={**element.metadata, "is_header": True})
    if element.table_id not in headers:
        return element
    header, source = headers[element.table_id]
    return replace(
        element,
        warnings=tuple(sorted({*element.warnings, "table_header_candidate"})),
        metadata={**element.metadata, "table_header": header, "context_sources": [source]},
    )


def _make_chunk(
    group: list[ParsedBlock],
    ordinal: int,
    start: int,
    end: int,
    section: tuple[str, ...],
    unit_id: str,
    *,
    pdf_layout: bool,
) -> TextChunk:
    separators = [_separator(a, b) for a, b in pairwise(group)] + [""]
    text = "".join(block.text + separator for block, separator in zip(group, separators, strict=True))
    sources: list[dict[str, Any]] = []
    cursor = 0
    for block, separator in zip(group, separators, strict=True):
        local_start, local_end = max(start, cursor), min(end, cursor + len(block.text))
        if local_start < local_end:
            locator = replace(
                block.locator,
                char_start=block.locator.char_start + local_start - cursor,
                char_end=block.locator.char_start + local_end - cursor,
            ).to_dict()
            if pdf_layout:
                locator.update({
                    key: block.metadata[key]
                    for key in ("pdf_layout", "pdf_column", "pdf_bbox", "pdf_component")
                    if block.metadata.get(key) is not None
                })
            sources.append(
                {
                    "chunk_start": local_start - start,
                    "chunk_end": local_end - start,
                    "locator": locator,
                }
            )
        cursor += len(block.text) + len(separator)
    locator = dict(sources[0]["locator"])
    if len(sources) > 1:
        # 顶层偏移不描述拼接后的原文；每段真实偏移保留在 sources 中。
        locator = {"type": group[0].locator.type, "precision": "composite", "sources": sources}
        pages = sorted({s["locator"]["page"] for s in sources if "page" in s["locator"]})
        if pages:
            locator["page"] = pages[0]
            locator["pages"] = pages
    else:
        locator["sources"] = sources
    piece = text[start:end]
    # 只补片段缺少的最近结构标签，避免文件名和整条路径稀释正文。
    label = section[-1] if section else ""
    context = [label] if label and label not in piece else []
    header = group[0].metadata.get("table_header")
    if header:
        context.append("表头：" + str(header))
    return TextChunk(
        ordinal,
        piece,
        locator,
        tuple(sorted({warning for block in group for warning in block.warnings})),
        "\n".join([*context, piece]),
        {
            "strategy": EVIDENCE_UNIT_STRATEGY,
            "kind": group[0].kind,
            "section_path": list(section),
            "unit_id": unit_id,
            "table_id": group[0].table_id,
            "is_header": group[0].metadata.get("is_header", False),
            "context_sources": group[0].metadata.get("context_sources", []),
            **({"pdf_layout": group[0].metadata.get("pdf_layout")} if pdf_layout else {}),
        },
    )


def _unit_connection(
    previous: ParsedBlock, current: ParsedBlock, *, in_section: bool, pdf_layout: bool,
) -> bool:
    a, b = previous.locator, current.locator
    if a.type != b.type or a.sheet != b.sheet:
        return False
    if not pdf_layout:
        return _compatible(previous, current) and (a.page == b.page or in_section)
    if pdf_continuation(previous, current, in_section):
        return True
    if a.page != b.page:
        # 基础 PDF 没有版面探测；保留已识别节内的相邻页未完句能力。
        return in_section and "pdf_bbox" not in previous.metadata and "pdf_bbox" not in current.metadata and (
            _compatible(previous, current)
        )
    if previous.metadata["pdf_layout"] == current.metadata["pdf_layout"] == "unknown":
        # 未知版面不推断栏位；仅凭显式节标题、列表或续文沿同页解析顺序装箱。
        previous_line = previous.text.lstrip()
        current_line = current.text.lstrip()
        return in_section or bool(
            (_LIST_ITEM.match(current_line) or _SUBPOINT.match(current_line)) and (
                previous.text.rstrip().endswith(("：", ":"))
                or _LIST_ITEM.match(previous_line) or _SUBPOINT.match(previous_line)
            )
        )
    return all(previous.metadata[key] == current.metadata[key] for key in ("pdf_band", "pdf_column"))


def _unit_atoms(block: ParsedBlock, cap: int, overlap: int) -> Iterator[ParsedBlock]:
    if len(block.text) <= cap:
        yield block
        return
    if block.kind in {"table", "table_row"}:
        spans = _split_text(block.text, cap, 0, table_rows=True)
    else:
        boundaries = [0, *(m.end() for m in _SENTENCE_BOUNDARY.finditer(block.text)), len(block.text)]
        spans = [
            (start + a, start + b)
            for start, end in pairwise(boundaries) if start < end
            for a, b in (
                [(0, end - start)] if end - start <= cap
                else _split_text(block.text[start:end], cap, min(overlap, cap // 4))
            )
        ]
    for start, end in spans:
        yield replace(block, text=block.text[start:end], locator=replace(
            block.locator, char_start=block.locator.char_start + start,
            char_end=block.locator.char_start + end,
        ))


def chunk_evidence_units(
    blocks: list[ParsedBlock], max_chars: int, overlap: int,
) -> list[TextChunk]:
    """先恢复语义单元，再在每个单元内优先以完整元素装箱。"""
    pdf_layout = bool(blocks) and all(block.locator.type == "pdf" for block in blocks)
    ordered = order_pdf_blocks(blocks) if pdf_layout else [
        block for _, block in sorted(enumerate(blocks), key=lambda item: (item[1].metadata.get("order", item[0]), item[0]))
    ]
    elements = (
        element for block in ordered for part in ([block] if pdf_layout else _table_rows(block))
        for element in _elements(part)
    )
    chunks: list[TextChunk] = []
    unit: list[ParsedBlock] = []
    unit_id = 0
    headings: list[tuple[int, str]] = []
    article = ""
    headers: dict[str, tuple[str, dict[str, Any]]] = {}
    omitted = False

    def flush() -> None:
        nonlocal unit_id
        if not unit:
            return
        path = tuple(label for _, label in headings) + ((article,) if article else ())
        packed: list[ParsedBlock] = []
        size = 0

        def emit() -> None:
            if packed and any(atom.text.strip() for atom in packed):
                chunks.append(_make_chunk(
                    packed, len(chunks), 0, size, path, str(unit_id),
                    pdf_layout=pdf_layout,
                ))
            packed.clear()

        for element in unit:
            for atom in _unit_atoms(element, max_chars, overlap):
                gap = len(_separator(packed[-1], atom)) if packed else 0
                if packed and size + gap + len(atom.text) > max_chars:
                    emit()
                    size = gap = 0
                packed.append(atom)
                size += gap + len(atom.text)
        emit()
        unit.clear()
        unit_id += 1

    for element in elements:
        if element.kind in {"header", "footer"}:
            omitted = True
            continue
        image_group = element.metadata.get("word_image_group")
        previous_group = unit[-1].metadata.get("word_image_group") if unit else None
        if image_group is not None or previous_group is not None:
            if image_group != previous_group:
                flush()
            if image_group is not None:
                # 同一张嵌入图的 OCR 段落共享文档单元；各段仍保留自己的来源偏移。
                unit.append(element)
                continue
        if element.kind in PDF_INDEPENDENT:
            flush()
            if element.text.strip():
                unit.append(_table_context(element, headers))
                flush()
            continue
        if not element.text.strip():
            continue
        if element.kind in {"heading", "article"}:
            flush()
            first_line = element.text.strip().splitlines()[0]
            marker = _HEADING.match(first_line)
            label = (marker[2] if marker else first_line).strip("*_` ")
            if element.kind == "heading":
                level = element.heading_level or 1
                while headings and headings[-1][0] >= level:
                    headings.pop()
                headings.append((level, label))
                article = ""
            else:
                match = _ARTICLE.match(label)
                article = match[0] if match else ""
        if unit:
            previous = unit[-1]
            if not _unit_connection(previous, element, in_section=bool(headings or article), pdf_layout=pdf_layout):
                flush()
            elif previous.locator.page != element.locator.page:
                element = replace(element, warnings=(*element.warnings, "cross_page_continuation_candidate"))
            elif pdf_layout and previous.metadata["pdf_column"] != element.metadata["pdf_column"]:
                element = replace(element, warnings=(*element.warnings, "cross_column_continuation_candidate"))
        unit.append(element)
    flush()
    if omitted:
        chunks = [replace(part, warnings=tuple(sorted({*part.warnings, "running_matter_omitted"}))) for part in chunks]
    return chunks
