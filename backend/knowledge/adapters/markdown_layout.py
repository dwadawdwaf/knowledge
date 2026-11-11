"""从围栏外的 Markdown 原文提取明确的管道表格及其码点范围。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from itertools import islice, pairwise

from knowledge.domain.ragflow_table import TableRow

_LINE = re.compile(r"[^\r\n]*(?:\r\n|[\r\n]|\Z)")
_PIPE = re.compile(r"\\.|\|")
_ALIGNMENT = re.compile(r":?-+:?")
_BLOCK_START = re.compile(r"(?:#{1,6}(?:[ \t]|$)|>|(?:[-+*]|\d{1,9}[.)])[ \t])")


def _cells(line: str) -> list[tuple[int, int]]:
    if "|" not in line:
        return []
    stripped = line.strip(" \t\r\n")
    left = len(line) - len(line.lstrip(" "))
    if left > 3 or line[left:left + 1] == "\t" or _BLOCK_START.match(stripped):
        return []
    right = left + len(stripped)
    pipes = list(islice((left + match.start() for match in _PIPE.finditer(stripped) if match.group() == "|"), 258))
    if not pipes or len(pipes) > 257:
        return []
    boundaries = [left - 1, *pipes, right]
    if pipes[0] == left:
        boundaries.pop(0)
    if pipes[-1] == right - 1:
        boundaries.pop()
    cells = [(start + 1, end) for start, end in pairwise(boundaries)]
    return cells if len(cells) <= 256 else []


def _located_cells(cells: list[tuple[int, int]], offset: int) -> list[tuple[int, int, int, int]]:
    return [(offset + start, offset + end, 1, 1) for start, end in cells]


def markdown_tables(text: str) -> Iterator[tuple[int, int, list[TableRow]]]:
    lines = (match for match in _LINE.finditer(text) if match.start() != match.end())
    current = next(lines, None)
    following = next(lines, None)
    while current is not None and following is not None:
        names, alignment = _cells(current.group()), _cells(following.group())
        if not names or len(names) != len(alignment) or not all(
            _ALIGNMENT.fullmatch(following.group()[start:end].strip()) for start, end in alignment
        ):
            current, following = following, next(lines, None)
            continue

        origin = current.start()
        end = following.end()
        rows: list[TableRow] = [(0, end - origin, True, _located_cells(names, 0))]
        cell_count = len(names)
        current = next(lines, None)
        while current is not None:
            data = _cells(current.group())
            if not data or len(rows) >= 4096 or cell_count + len(data) > 16384:
                break
            end = current.end()
            offset = current.start() - origin
            rows.append((offset, end - origin, False, _located_cells(data, offset)))
            cell_count += len(data)
            current = next(lines, None)
        yield origin, end, rows
        following = next(lines, None)
