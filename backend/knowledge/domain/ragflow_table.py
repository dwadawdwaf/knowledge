"""将解析器明确给出的合并表头转换为列路径，保留真实数据行。"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace
from itertools import takewhile

from knowledge.domain.entities import ParsedBlock

TableRow = tuple[int, int, bool, list[tuple[int, int, int, int]]]
_SHEET_DATA = re.compile(r"[+-]?\d[\d.,%:/ -]*|0x[0-9a-f]+|[a-z]+[-_]*\d+(?:[-_.][a-z\d]+)*", re.IGNORECASE)


def _header_paths(grids: list[dict[int, str]]) -> list[str]:
    columns = {column for grid in grids for column in grid}
    if set(grids[-1]) != columns or columns != set(range(len(columns))):
        return []
    return ["-".join(dict.fromkeys(grid[column] for grid in grids if grid.get(column)))
            for column in range(len(columns))]


def docx_table_rows(elements: list[ParsedBlock]) -> list[ParsedBlock]:
    tables: dict[str, list[ParsedBlock]] = {}
    for block in elements:
        if block.table_id and "docx_grid" in block.metadata:
            tables.setdefault(block.table_id, []).append(block)

    layouts: dict[str, tuple[int, list[str]]] = {}
    for table_id, rows in tables.items():
        headings = list(takewhile(lambda row: row.metadata["docx_header"], rows))
        if not headings or any(
            row.metadata["row"] != number or row.metadata["docx_grid"] is None
            for number, row in enumerate(headings, start=1)
        ):
            continue
        labels = _header_paths([
            {column: " ".join(value.split()) for column, (value, _) in enumerate(row.metadata["docx_grid"])}
            for row in headings
        ])
        if labels and all(labels) and len(set(labels)) == len(labels):
            layouts[table_id] = len(headings), labels

    result = []
    for block in elements:
        if "docx_grid" not in block.metadata:
            result.append(block)
            continue
        number = block.metadata["row"]
        grid = block.metadata["docx_grid"]
        depth, labels = layouts.get(block.table_id or "", (0, []))
        header = ""
        fields: list[str] = []
        if labels and number > depth and not block.metadata["docx_header"] and grid and len(grid) == len(labels):
            header = "\t".join(labels)
            for label, (value, direct) in zip(labels, grid, strict=True):
                if direct and value:
                    fields.append(f"{label}: {' '.join(value.split())}")
        result.append(replace(
            block, locator=replace(block.locator, row_start=number, row_end=number),
            metadata={**block.metadata, "table_header": header, "table_fields": fields},
        ))
    return result


def _column_fields(text: str, rows: list[TableRow]) -> tuple[str, dict[int, list[str]]]:
    head_count = sum(1 for _ in takewhile(lambda row: row[2], rows))
    if not 0 < head_count < len(rows):
        return "", {}

    occupied: dict[int, tuple[int, str]] = {}
    grids: list[dict[int, str]] = []
    labels: list[str] = []
    fields: dict[int, list[str]] = {}
    header = ""
    for number, (_, _, is_header, cells) in enumerate(rows):
        occupied = {column: cell for column, cell in occupied.items() if cell[0] > number}
        direct: dict[int, str] = {}
        cursor = 0
        for start, end, rowspan, colspan in cells:
            while cursor in occupied:
                cursor += 1
            columns = range(cursor, cursor + colspan)
            if columns.stop > 256 or any(column in occupied for column in columns) or number + rowspan > len(rows):
                return "", {}
            if number < head_count < number + rowspan:
                return "", {}
            value = " ".join(text[start:end].split())
            occupied.update((column, (number + rowspan, value)) for column in columns)
            if colspan == 1 and value:
                direct[cursor] = value
            cursor = columns.stop
        if number < head_count:
            grids.append({column: value for column, (_, value) in occupied.items()})
            if number == head_count - 1:
                labels = _header_paths(grids)
                if not labels:
                    return "", {}
                header = "\t".join(labels)
        elif not is_header and set(occupied) == set(range(len(labels))):
            # 合并或跨行继承的值缺少单列或本行出处，只增强本行中有明确列归属的单元格。
            fields[number] = [
                f"{labels[column]}: {value}" for column, value in direct.items() if labels[column]
            ]
    return header, fields


def structured_table_rows(block: ParsedBlock) -> list[ParsedBlock]:
    rows: list[TableRow] = block.metadata["table_rows"]
    if not rows:
        return [block]
    header, fields = _column_fields(block.text, rows)
    result: list[ParsedBlock] = []

    def append(start: int, end: int, row_index: int | None) -> None:
        source = block.text[start:end]
        text = source.strip()
        if not text:
            return
        start += len(source) - len(source.lstrip())
        result.append(replace(
            block, kind="table_row" if row_index is not None else "table", text=text,
            locator=replace(block.locator, char_start=block.locator.char_start + start,
                            char_end=block.locator.char_start + start + len(text)),
            metadata={**block.metadata, "table_header": header if row_index is not None and row_index in fields else "",
                      "table_fields": fields.get(row_index, []) if row_index is not None else []},
        ))

    previous = 0
    for index, (start, end, _, _) in enumerate(rows):
        append(previous, start, None)
        append(start, end, index)
        previous = end
    append(previous, len(block.text), None)
    return result or [block]


@dataclass(frozen=True)
class XlsxTableLayout:
    labels: list[str]
    last_header_row: int
    wide_cells: set[tuple[int, int]]


def xlsx_table_layout(block: ParsedBlock) -> XlsxTableLayout | None:
    preview: list[list[str]] = block.metadata["xlsx_preview"]
    ranges: list[tuple[int, int, int, int]] = block.metadata["xlsx_merge_ranges"]

    def data_like(value: str) -> bool:
        return _SHEET_DATA.fullmatch(value) is not None or value.upper() in {"Y", "N", "M", "X", "/", "-"}

    def header_like(row: list[str]) -> bool:
        return not any(data_like(value) for value in row if value) and any(
            sum(char.isalpha() for char in value) >= 2 for value in row
        )

    header_rows = list(takewhile(header_like, preview[:5]))
    depth = len(header_rows)
    sample = next((row for row in preview[depth:] if any(row)), [])
    if not header_rows or not any(data_like(value) for value in sample if value):
        return None

    header_ranges = [area for area in ranges if area[1] <= depth]
    if not header_ranges or any(area[2] > 256 or area[3] > depth for area in header_ranges):
        return None
    width = max(
        max((column for row in header_rows for column, value in enumerate(row, 1) if value), default=0),
        max(area[2] for area in header_ranges),
    )
    grid = [{column: " ".join(value.split()) for column, value in enumerate(row[:width])}
            for row in header_rows]
    covered: set[tuple[int, int]] = set()
    for first_col, first_row, last_col, last_row in header_ranges:
        anchor = first_row - 1, first_col - 1
        label = grid[anchor[0]].get(anchor[1], "")
        if not label:
            return None
        for row in range(first_row - 1, last_row):
            for column in range(first_col - 1, last_col):
                location = row, column
                if location in covered or (location != anchor and grid[row].get(column)):
                    return None
                covered.add(location)
                grid[row][column] = label
    labels = _header_paths(grid)
    if not labels or not all(labels) or len(set(labels)) != len(labels):
        return None
    wide_cells = {(row, first_col - 1) for first_col, row, last_col, _ in ranges if first_col < last_col}
    return XlsxTableLayout(labels, depth, wide_cells)


def xlsx_table_rows(block: ParsedBlock, rows: list[ParsedBlock], layout: XlsxTableLayout | None) -> list[ParsedBlock]:
    lengths: list[list[int] | None] = block.metadata.get("xlsx_cell_lengths", [])
    result: list[ParsedBlock] = []
    for index, row in enumerate(rows):
        fields: list[str] = []
        header = ""
        number = row.locator.row_start
        if layout and number is not None and number > layout.last_header_row and index < len(lengths):
            row_lengths = lengths[index]
            if row_lengths is None:
                values = row.text.split("\t")
            else:
                values = []
                start = 0
                for length in row_lengths:
                    values.append(row.text[start:start + length])
                    start += length + 1
            if values and not any(values[len(layout.labels):]):
                header = "\t".join(layout.labels)
                for column, (label, cell) in enumerate(zip(layout.labels, values, strict=False)):
                    value = " ".join(cell.split())
                    if value and (number, column) not in layout.wide_cells:
                        fields.append(f"{label}: {value}")
        result.append(replace(row, metadata={**row.metadata, "table_header": header, "table_fields": fields}))
    return result
