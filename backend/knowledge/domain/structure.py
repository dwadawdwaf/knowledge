"""按文档元素组织片段，保存合并前的每个原文区间；无 I/O。"""

from __future__ import annotations

import re
from dataclasses import replace
from itertools import pairwise
from typing import Any

from knowledge.domain.chunking import TextChunk, _split_text
from knowledge.domain.entities import ParsedBlock
from knowledge.domain.pdf_layout import (
    PDF_INDEPENDENT,
    PDF_LAYOUT_STRATEGY,
    order_pdf_blocks,
    pdf_continuation,
)

_HEADING = re.compile(r"^(#{1,6})\s+(.+)$")
_CHAPTER = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+[章节编部]\s*\S.*$")
_ARTICLE = re.compile(r"^第[一二三四五六七八九十百千万零〇\d]+条")


def _elements(block: ParsedBlock, *, pdf_layout: bool = False) -> list[ParsedBlock]:
    """识别行首显式结构，偏移仍属于原始解析块。"""
    if block.kind in {"table", "table_row", "figure", "caption", "footnote", "header", "footer"} or (
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
            boundaries.add(line.start())
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


def _make_chunk(
    group: list[ParsedBlock],
    ordinal: int,
    start: int,
    end: int,
    section: tuple[str, ...],
    unit_id: str,
    title: str,
    *,
    pdf_layout: bool,
) -> TextChunk:
    text = "\n".join(block.text for block in group)
    sources: list[dict[str, Any]] = []
    cursor = 0
    for block in group:
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
        cursor += len(block.text) + 1
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
    context: list[str] = []
    if title:
        context.append(title)
    context.extend(section)
    header = group[0].metadata.get("table_header")
    if header:
        context.append("表头：" + str(header))
    piece = text[start:end]
    return TextChunk(
        ordinal,
        piece,
        locator,
        tuple(sorted({warning for block in group for warning in block.warnings})),
        "\n".join([*context, piece]),
        {
            "strategy": PDF_LAYOUT_STRATEGY if pdf_layout else "structure-v2",
            "kind": group[0].kind,
            "section_path": list(section),
            "unit_id": unit_id,
            "table_id": group[0].table_id,
            "is_header": group[0].metadata.get("is_header", False),
            "context_sources": group[0].metadata.get("context_sources", []),
            **({"pdf_layout": group[0].metadata.get("pdf_layout")} if pdf_layout else {}),
        },
    )


def chunk_structured(
    blocks: list[ParsedBlock],
    max_chars: int,
    overlap: int,
    title: str,
    *,
    pdf_layout: bool = False,
) -> list[TextChunk]:
    pdf_layout = pdf_layout and bool(blocks) and all(block.locator.type == "pdf" for block in blocks)
    if pdf_layout:
        blocks = order_pdf_blocks(blocks)
    else:
        blocks = [block for _, block in sorted(
            enumerate(blocks), key=lambda item: (item[1].metadata.get("order", item[0]), item[0])
        )]
    elements = [
        element
        for block in blocks
        for part in ([block] if pdf_layout else _table_rows(block))
        for element in _elements(part, pdf_layout=pdf_layout)
    ]
    chunks: list[TextChunk] = []
    section: list[str] = []
    article = ""
    group: list[ParsedBlock] = []
    previous_pdf: ParsedBlock | None = None
    unit = 0
    table_headers: dict[str, tuple[str, dict[str, Any]]] = {}
    omitted_running_matter = False

    def flush() -> None:
        nonlocal group
        if not group:
            return
        text = "\n".join(block.text for block in group)
        prose = pdf_layout and group[0].kind == "text"
        table = pdf_layout and group[0].kind == "table"
        cap = min(max_chars, 520) if prose and group[0].metadata["pdf_layout"] != "single_column" else max_chars
        effective_overlap = min(overlap, cap // 4) if prose else 0 if table else overlap
        for start, end in _split_text(
            text, cap, effective_overlap, sentence_boundaries=prose, table_rows=table,
        ):
            if text[start:end].strip():
                path = tuple(section) + ((article,) if article else ())
                chunks.append(_make_chunk(
                    group, len(chunks), start, end, path, str(unit), title, pdf_layout=pdf_layout,
                ))
        group = []

    for element in elements:
        if element.kind in {"header", "footer"}:
            omitted_running_matter = True
            continue
        if not element.text.strip():
            if pdf_layout and element.kind in PDF_INDEPENDENT:
                flush()
                unit += 1
                previous_pdf = None
            continue
        if element.kind == "table_row" and element.table_id:
            cells = re.sub(r"^表格\d+ 第\d+行：", "", element.text).split(
                "\t" if "\t" in element.text else "｜"
            )
            row = element.metadata.get("row", element.locator.row_start)
            if (
                row == 1
                and len(cells) > 1
                and all(
                    cell.strip() and not re.fullmatch(r"[\d.,%￥$+-]+", cell.strip())
                    for cell in cells
                )
            ):
                table_headers[element.table_id] = (element.text, element.locator.to_dict())
                element = replace(element, metadata={**element.metadata, "is_header": True})
            if element.table_id in table_headers:
                header, source = table_headers[element.table_id]
                element = replace(
                    element,
                    warnings=tuple(sorted({*element.warnings, "table_header_candidate"})),
                    metadata={
                        **element.metadata,
                        "table_header": header,
                        "context_sources": [source],
                    },
                )
        if element.kind == "heading":
            flush()
            unit += 1
            article = ""
            level = max(1, element.heading_level or 1)
            section = section[: level - 1] + [element.text.strip()]
        elif element.kind == "article":
            flush()
            unit += 1
            match = _ARTICLE.match(element.text.strip())
            article = match[0] if match else ""
        if pdf_layout:
            continuation = previous_pdf is not None and pdf_continuation(
                previous_pdf, element, bool(section or article)
            )
            if previous_pdf is not None and not continuation and (
                previous_pdf.locator.page != element.locator.page
                or previous_pdf.metadata["pdf_band"] != element.metadata["pdf_band"]
                or previous_pdf.metadata["pdf_column"] != element.metadata["pdf_column"]
            ):
                flush()
                unit += 1
            if group:
                previous = group[-1]
                size = sum(len(block.text) + 1 for block in group) + len(element.text)
                cap = max_chars if all(
                    item.metadata["pdf_layout"] == "single_column" for item in (group[0], element)
                ) else min(max_chars, 520)
                if not continuation or size > cap:
                    flush()
                elif previous.locator.page != element.locator.page:
                    element = replace(element, warnings=tuple(sorted({
                        *element.warnings, "cross_page_continuation_candidate",
                    })))
                elif previous.metadata["pdf_column"] != element.metadata["pdf_column"]:
                    element = replace(element, warnings=tuple(sorted({
                        *element.warnings, "cross_column_continuation_candidate",
                    })))
            group.append(element)
            if element.kind != "text":
                flush()
                unit += 1
            previous_pdf = element
            continue
        if group:
            size = sum(len(block.text) + 1 for block in group) + len(element.text)
            cross_page_unknown = group[-1].locator.page != element.locator.page and not (
                section or article
            )
            compatible = _compatible(group[-1], element)
            if not compatible or size > max_chars or cross_page_unknown:
                previous = group[-1]
                flush()
                if (
                    cross_page_unknown
                    or previous.kind in {"table", "table_row"}
                    or previous.locator.type != element.locator.type
                ):
                    unit += 1
            elif group[-1].locator.page != element.locator.page:
                element = replace(
                    element,
                    warnings=tuple(
                        sorted({*element.warnings, "cross_page_continuation_candidate"})
                    ),
                )
        group.append(element)
        if element.kind in {"table", "table_row", "figure", "caption", "footnote"}:
            flush()
    flush()
    if omitted_running_matter:
        chunks = [
            replace(piece, warnings=tuple(sorted({*piece.warnings, "running_matter_omitted"})))
            for piece in chunks
        ]
    return chunks
