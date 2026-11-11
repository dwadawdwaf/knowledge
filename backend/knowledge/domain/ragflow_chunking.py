"""按标题层级和 token 预算组织解析块，并保留每个来源的码点范围。"""

from __future__ import annotations

import re
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field, replace

from knowledge.domain.chunking import TextChunk
from knowledge.domain.entities import ParsedBlock
from knowledge.domain.pdf_layout import order_pdf_blocks
from knowledge.domain.ragflow_table import (
    XlsxTableLayout,
    docx_table_rows,
    structured_table_rows,
    xlsx_table_layout,
    xlsx_table_rows,
)
from knowledge.domain.structure import _elements, _table_rows

STRATEGY = "ragflow-v1"
TOKEN_CAP = 512
TITLE_DEPTH = 3
_SENTENCE = re.compile(r"(?:[!?;。！？；]+|(?<!\.)\.+(?=\s|$)|\r?\n)(?:[ \t]*\r?\n)*")
_CODE_LINE = re.compile(r"(?:\r\n|[\r\n])(?:[ \t]*(?:\r\n|[\r\n]))*")
_INDEPENDENT = {"table", "table_row", "caption", "figure", "footnote", "equation", "code", "aside"}
_NUMBER = re.compile(r"[\d.,%￥$+−-]+")


def _rows(block: ParsedBlock, xlsx_layouts: dict[str, XlsxTableLayout | None]) -> list[ParsedBlock]:
    if block.kind != "table":
        return [block]
    if "table_rows" in block.metadata:
        return structured_table_rows(block)
    if "csv_row_spans" in block.metadata:
        rows = []
        for number, (start, end, lengths) in zip(
            block.metadata["row_numbers"], block.metadata["csv_row_spans"], strict=True,
        ):
            cells = []
            cursor = start
            for length in lengths:
                cells.append(block.text[cursor:cursor + length])
                cursor += length + 1
            rows.append(replace(
                block, kind="table_row", text=block.text[start:end],
                locator=replace(block.locator, row_start=number, row_end=number,
                                char_start=block.locator.char_start + start,
                                char_end=block.locator.char_start + end),
                metadata={**block.metadata, "csv_cells": cells},
            ))
        return rows
    if "row_numbers" in block.metadata:
        rows = _table_rows(block)
        if block.locator.type == "xlsx" and block.table_id is not None and block.table_id in xlsx_layouts:
            return xlsx_table_rows(block, rows, xlsx_layouts[block.table_id])
        return rows
    if "\t" not in block.text and "\n" not in block.text:
        return [block]
    return [
        replace(
            block, kind="table_row", text=row.group(),
            locator=replace(block.locator, char_start=block.locator.char_start + row.start(),
                            char_end=block.locator.char_start + row.end()),
            metadata={**block.metadata, "is_header": index == 1 and block.metadata.get("first_row_header", False)},
        )
        for index, row in enumerate(re.finditer(r"[^\r\n]+", block.text), start=1)
        if row.group().strip()
    ]


def _separator_ranges(text: str, delimiter: re.Pattern[str]) -> Iterator[tuple[int, int]]:
    """分隔符保留在源区间内，空行归入相邻正文。"""
    start = 0
    pending: tuple[int, int] | None = None
    for match in delimiter.finditer(text):
        end = match.end()
        if not text[start:end].strip():
            continue
        if pending is not None:
            yield pending
        pending = (start, end)
        start = end
    if pending is None:
        yield 0, len(text)
    elif text[start:].strip():
        yield pending
        yield start, len(text)
    else:
        yield pending[0], len(text)


def _spans(text: str, count: Callable[[str], int], *, kind: str) -> Iterator[tuple[int, int]]:
    if kind != "text" and len(text) <= TOKEN_CAP * 8 and count(text) <= TOKEN_CAP:
        yield 0, len(text)
        return
    delimiter = _CODE_LINE if kind == "code" else _SENTENCE
    for start, end in _separator_ranges(text, delimiter):
        while start < end:
            window_end = min(end, start + TOKEN_CAP * 8)
            if count(text[start:window_end]) <= TOKEN_CAP:
                yield start, window_end
                start = window_end
                continue
            low, high = start + 1, window_end - 1
            while low < high:
                middle = (low + high + 1) // 2
                if count(text[start:middle]) <= TOKEN_CAP:
                    low = middle
                else:
                    high = middle - 1
            yield start, low
            start = low


def _cells(block: ParsedBlock) -> list[str]:
    if "csv_cells" in block.metadata:
        return block.metadata["csv_cells"]
    body = re.sub(r"^表格\d+ 第\d+行：", "", block.text)
    return [cell.strip() for cell in body.split("\t" if "\t" in body else "｜")]


def _table_headers(elements: list[ParsedBlock]) -> dict[str, ParsedBlock]:
    by_table: dict[str, list[ParsedBlock]] = {}
    for block in elements:
        if block.kind == "table_row" and block.table_id:
            rows = by_table.setdefault(block.table_id, [])
            if len(rows) < 2:
                rows.append(block)
    headers: dict[str, ParsedBlock] = {}
    for table_id, rows in by_table.items():
        first = rows[0]
        if first.metadata.get("is_header"):
            headers[table_id] = first
            continue
        if len(rows) != 2 or first.locator.type not in {"csv", "xlsx"} or first.locator.row_start != 1:
            continue
        names, values = map(_cells, rows)
        if (
            len(names) == len(values) >= 2 and all(names) and len(set(names)) == len(names)
            and all(not _NUMBER.fullmatch(name) for name in names)
            and any(_NUMBER.fullmatch(value) for value in values)
        ):
            headers[table_id] = first
    return headers



@dataclass
class _Section:
    level: int
    heading: ParsedBlock | None = None
    body: list[ParsedBlock] = field(default_factory=list)
    children: list[_Section] = field(default_factory=list)


_SectionBlock = tuple[ParsedBlock, tuple[str, ...]]


def _separator(previous: str, current: str) -> str:
    return "\n" if previous and not previous.endswith("\n") and not current.startswith(("\n", "\r\n")) else ""


def _common_path(left: tuple[str, ...], right: tuple[str, ...]) -> tuple[str, ...]:
    for index, (a, b) in enumerate(zip(left, right, strict=False)):
        if a != b:
            return left[:index]
    return left[:min(len(left), len(right))]


def _hierarchical_runs(
    elements: list[ParsedBlock], count: Callable[[str], int],
) -> Iterator[tuple[list[_SectionBlock], list[_SectionBlock]]]:
    """按 RAGFlow HierarchicalMerger 的父子路径输出来源块。"""
    root = _Section(0)
    stack = [root]
    for element in elements:
        if element.kind not in {"heading", "article"}:
            stack[-1].body.append(element)
            continue
        level = min(7, max(1, element.heading_level or (7 if element.kind == "article" else 1)))
        while stack[-1].level >= level:
            stack.pop()
        section = _Section(level, element)
        stack[-1].children.append(section)
        stack.append(section)

    if root.body:
        yield [], [(block, ()) for block in root.body]

    def title(section: _Section) -> str:
        assert section.heading is not None
        return section.heading.text.strip()[:512]

    def records(section: _Section, path: tuple[str, ...]) -> Iterator[_SectionBlock]:
        assert section.heading is not None
        yield section.heading, path
        yield from ((block, path) for block in section.body)
        for child in section.children:
            yield from records(child, (*path, title(child)))

    emitted: set[int] = set()

    def run(
        prefix: list[_SectionBlock], local: list[_SectionBlock],
    ) -> tuple[list[_SectionBlock], list[_SectionBlock]]:
        emitted.update(id(block) for block, _ in (*prefix, *local))
        return prefix, local

    def fits_before_children(prefix: list[_SectionBlock], children: list[_Section]) -> bool:
        body = ""
        for block, _ in prefix:
            body += _separator(body, block.text) + block.text
            if count(body) > TOKEN_CAP:
                return False
        for child in children:
            assert child.heading is not None
            heading = child.heading.text
            if count(body + _separator(body, heading) + heading) > TOKEN_CAP:
                return False
        return True

    def visit(
        section: _Section, inherited: list[_SectionBlock], parents: tuple[str, ...],
    ) -> Iterator[tuple[list[_SectionBlock], list[_SectionBlock]]]:
        assert section.heading is not None
        titles = (*parents, title(section))
        if not section.children or len(titles) == TITLE_DEPTH:
            yield run(inherited, list(records(section, titles)))
            return

        local = [(block, titles) for block in (section.heading, *section.body)]
        prefix = [*inherited, *(item for item in local if item[0].kind not in _INDEPENDENT)]
        if not fits_before_children(prefix, section.children):
            # 尚未进入分块的祖先引言随当前引言保存一次，子节优先使用自身正文。
            unseen = [item for item in inherited if id(item[0]) not in emitted]
            yield run([], [*unseen, *local])
            heading_only = [local[0]]
            prefix = heading_only if fits_before_children(heading_only, section.children) else []
        else:
            for item in local:
                if item[0].kind in _INDEPENDENT:
                    yield run([], [item])
        for child in section.children:
            yield from visit(child, prefix, titles)

    for section in root.children:
        yield from visit(section, [], ())


def chunk_ragflow(
    blocks: list[ParsedBlock], count: Callable[[str], int], *, mineru_pdf: bool = False,
) -> list[TextChunk]:
    ordered = order_pdf_blocks(blocks) if mineru_pdf else sorted(
        blocks, key=lambda block: block.metadata.get("order", 0),
    )
    xlsx_layouts = {
        block.table_id: xlsx_table_layout(block) for block in ordered
        if block.table_id and "xlsx_merge_ranges" in block.metadata
    }
    elements = [row for block in ordered for element in _elements(block, pdf_layout=mineru_pdf)
                for row in _rows(element, xlsx_layouts) if row.text.strip()]
    elements = docx_table_rows(elements)
    headers = _table_headers(elements)
    title_mode = any(element.kind in {"heading", "article"} for element in elements)
    chunks: list[TextChunk] = []
    omitted = any(block.kind in {"header", "footer"} for block in ordered)
    content = [element for element in elements if element.kind not in {"header", "footer"}]

    def pack(prefix: list[_SectionBlock], run: list[_SectionBlock]) -> None:
        parts: list[tuple[ParsedBlock, int, int, str]] = []
        body = ""
        token_total = 0
        last: ParsedBlock | None = None
        chunk_path: tuple[str, ...] | None = None
        prefix_path: tuple[str, ...] = ()

        def flush() -> None:
            nonlocal body, token_total, chunk_path, prefix_path
            if not parts:
                return
            sources: list[dict] = []
            warnings = {"running_matter_omitted"} if omitted else set()
            offset = 0
            for block, start, end, sep in parts:
                offset += len(sep)
                loc = replace(
                    block.locator, char_start=block.locator.char_start + start,
                    char_end=block.locator.char_start + end,
                ).to_dict()
                loc.update({key: block.metadata[key] for key in ("pdf_bbox", "pdf_column", "pdf_component")
                            if block.metadata.get(key) is not None})
                sources.append({"chunk_start": offset, "chunk_end": offset + end - start, "locator": loc})
                offset += end - start
                warnings.update(block.warnings)
            locator = {**sources[0]["locator"], "sources": sources}
            if len(sources) > 1:
                locator = {"type": sources[0]["locator"]["type"], "precision": "composite", "sources": sources}
                pages = sorted({item["locator"]["page"] for item in sources if "page" in item["locator"]})
                if pages:
                    locator.update(page=pages[0], pages=pages)
            first = parts[0][0]
            header_row = headers.get(first.table_id or "")
            header = first.metadata.get("table_header", header_row.text if header_row is not None else "")
            if "csv_cells" in first.metadata and header_row is not None and len(_cells(first)) != len(_cells(header_row)):
                header = ""
            if header == first.text:
                header = ""
            fields: list[str] = []
            if "table_fields" in first.metadata and body == first.text:
                fields = first.metadata["table_fields"]
            elif (
                "table_fields" not in first.metadata and header and header_row is not None
                and first.kind == "table_row" and body == first.text
            ):
                labels, cells = _cells(header_row), _cells(first)
                if len(labels) == len(cells):
                    fields = [
                        f"{label}: {cell}" for label, cell in zip(labels, cells, strict=True)
                        if label and cell
                    ]
            chunks.append(TextChunk(
                len(chunks), body, locator, tuple(sorted(warnings)),
                structure={"strategy": STRATEGY, "kind": first.kind,
                           "section_path": list(chunk_path if chunk_path is not None else prefix_path),
                           "table_id": first.table_id,
                           "table_header": header, "table_fields": fields},
            ))
            parts.clear()
            body = ""
            token_total = 0
            chunk_path = None
            prefix_path = ()

        for index, (block, path) in enumerate((*prefix, *run)):
            independent = block.kind in _INDEPENDENT
            if independent or (
                last is not None and (
                    block.locator.type != last.locator.type
                    or block.locator.type == "pptx" and block.locator.page != last.locator.page
                    or block.locator.type == "pdf" and block.locator.page != last.locator.page and (
                        block.locator.page != (last.locator.page or 0) + 1
                        or mineru_pdf and block.metadata.get("pdf_layout") == "unknown"
                    )
                )
            ):
                flush()
            for start, end in _spans(block.text, count, kind=block.kind):
                fragment = block.text[start:end]
                if not fragment.strip():
                    continue
                separator = _separator(body, fragment) if parts and parts[-1][0] is not block else ""
                # 上游 naive_merge 在当前块已超过目标后才开启下一个块。
                # 标题、独立块仍使用本项目的上限与定位约束。
                if parts and (
                    count(body + separator + fragment) > TOKEN_CAP
                    if title_mode or independent else token_total > TOKEN_CAP
                ):
                    flush()
                    separator = ""
                body += separator + fragment
                if index < len(prefix):
                    prefix_path = path
                else:
                    chunk_path = path if chunk_path is None else _common_path(chunk_path, path)
                if not title_mode:
                    token_total += count(separator + fragment)
                if parts and parts[-1][0] is block and parts[-1][2] == start:
                    original, first_start, _, first_separator = parts[-1]
                    parts[-1] = (original, first_start, end, first_separator)
                else:
                    parts.append((block, start, end, separator))
            if independent:
                flush()
            last = block
        flush()

    if title_mode:
        for prefix, run in _hierarchical_runs(content, count):
            pack(prefix, run)
    else:
        pack([], [(block, ()) for block in content])
    return chunks

