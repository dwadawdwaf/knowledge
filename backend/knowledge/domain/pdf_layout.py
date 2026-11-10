"""从解析块恢复 PDF 的页内阅读顺序，歧义时保留解析器原序。"""

from __future__ import annotations

import math
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass, replace
from itertools import pairwise
from statistics import median

from knowledge.domain.entities import ParsedBlock

PDF_INDEPENDENT = frozenset(
    {"table", "table_row", "figure", "caption", "footnote", "equation", "code", "aside"}
)
_MARGINAL = frozenset({"header", "footer"})
_SENTENCE_END = ".!?。！？；;：:"


@dataclass(frozen=True)
class _Box:
    index: int
    left: float
    top: float
    right: float
    bottom: float


def _box(index: int, block: ParsedBlock) -> _Box | None:
    coordinates = block.metadata.get("pdf_bbox")
    if coordinates is None and block.locator.positions:
        positions = block.locator.positions
        if any(position[0] != block.locator.page for position in positions):
            return None
        coordinates = (
            min(position[1] for position in positions),
            min(position[3] for position in positions),
            max(position[2] for position in positions),
            max(position[4] for position in positions),
        )
    if (
        not isinstance(coordinates, (list, tuple))
        or len(coordinates) != 4
        or not all(
            type(value) in (int, float) and 0 <= value and math.isfinite(value)
            for value in coordinates
        )
    ):
        return None
    left, top, right, bottom = coordinates
    return _Box(index, left, top, right, bottom) if left < right and top < bottom else None


def _columns(
    boxes: list[_Box], body: list[_Box], left_edge: float, width: float
) -> tuple[list[_Box], list[_Box], list[_Box]] | None:
    # 用页面内容宽度寻找中部栏间空隙，允许不等宽两栏。窄侧注不作为正文栏。
    body_indices = {box.index for box in body}
    for fraction in (0.50, 0.42, 0.58, 0.35, 0.65):
        cut = left_edge + fraction * width
        left = [box for box in body if box.right <= cut]
        right = [box for box in body if box.left >= cut]
        if not left or not right:
            continue
        left_end = max(box.right for box in left)
        right_start = min(box.left for box in right)
        if right_start - left_end < 0.025 * width:
            continue
        if any(
            median(box.right - box.left for box in side) < 0.22 * width
            or max(box.left for box in side) - min(box.left for box in side) > 0.13 * width
            for side in (left, right)
        ):
            continue
        if min(max(box.bottom for box in side) for side in (left, right)) <= max(
            min(box.top for box in side) for side in (left, right)
        ):
            continue
        left_blocks = [box for box in boxes if box.right <= cut]
        right_blocks = [box for box in boxes if box.left >= cut]
        spanning = [box for box in boxes if box.left < cut < box.right]
        if any(box.index in body_indices and box.right - box.left < 0.65 * width for box in spanning):
            continue
        return left_blocks, right_blocks, spanning
    return None


def _reading_order(
    boxes: list[_Box], body: list[_Box]
) -> tuple[str, list[tuple[int, int, str]]] | None:
    if not body:
        return None
    left = min(box.left for box in body)
    width = max(box.right for box in body) - left
    columns = _columns(boxes, body, left, width)
    if columns is None:
        if max(box.left for box in body) - left > 0.15 * width:
            return None
        single = sorted(boxes, key=lambda box: (box.top, box.left, box.index))
        return "single_column", [(box.index, 0, "single") for box in single]

    left_boxes, right_boxes, spanning = columns
    spanning.sort(key=lambda box: (box.top, box.index))
    if any(a.bottom > b.top for a, b in pairwise(spanning)):
        return None
    ends = [box.bottom for box in spanning]
    flows: dict[int, list[tuple[int, _Box, str]]] = defaultdict(list)
    for lane, group in (("left", left_boxes), ("right", right_boxes)):
        for box in group:
            preceding = bisect_right(ends, box.top)
            if preceding < len(spanning) and box.bottom > spanning[preceding].top:
                return None
            region = 2 * preceding
            flows[region].append((int(lane == "right"), box, lane))
    for number, box in enumerate(spanning):
        flows[2 * number + 1].append((0, box, "full"))
    return ("mixed" if spanning else "two_column"), [
        (box.index, region, lane)
        for region in sorted(flows)
        for _, box, lane in sorted(
            flows[region], key=lambda item: (item[0], item[1].top, item[1].index)
        )
    ]


def _caption_blocks(block: ParsedBlock) -> list[ParsedBlock]:
    captions = block.metadata.get("pdf_captions")
    if not isinstance(captions, list):
        return []
    result: list[ParsedBlock] = []
    for index, caption in enumerate(captions):
        if not isinstance(caption, dict):
            continue
        value = caption.get("content")
        if not isinstance(value, str) or not value.strip():
            continue
        text = value.replace("\x00", "\ufffd")
        result.append(
            replace(
                block,
                text=text,
                kind="caption",
                heading_level=None,
                table_id=None,
                locator=replace(block.locator, char_start=0, char_end=len(text), positions=()),
                metadata={
                    **block.metadata,
                    "pdf_component": f"caption:{index}",
                    "pdf_bbox": caption.get("bbox"),
                    "pdf_continues_prev": False,
                },
            )
        )
    return result


def order_pdf_blocks(blocks: list[ParsedBlock]) -> list[ParsedBlock]:
    pages: dict[int, list[ParsedBlock]] = defaultdict(list)
    previous = None
    for index, block in enumerate(blocks):
        kind = block.metadata.get("pdf_kind", block.kind)
        if kind in _MARGINAL:
            # 保留已有页眉页脚告警流程，版面判断排除这些辅助块。
            pages[block.locator.page or 0].append(replace(block, kind=kind))
            continue
        metadata = {**block.metadata, "pdf_original_index": index, "pdf_previous_index": previous}
        pages[block.locator.page or 0].append(replace(block, kind=kind, metadata=metadata))
        previous = index

    result: list[ParsedBlock] = []
    for page in sorted(pages):
        entries = pages[page]
        active = [index for index, block in enumerate(entries) if block.kind not in _MARGINAL]
        boxes = [box for index in active if (box := _box(index, entries[index])) is not None]
        layout = None
        if page and boxes and len(boxes) == len(active):
            body = [box for box in boxes if entries[box.index].kind == "text"]
            layout = _reading_order(boxes, body)
        if layout is None:
            name, order = "unknown", [(index, index, "unknown") for index in active]
        else:
            name, order = layout
        result.extend(block for block in entries if block.kind in _MARGINAL)
        for index, band, column in order:
            block = entries[index]
            for item in [block, *_caption_blocks(block)]:
                warnings = set(item.warnings)
                if name == "unknown":
                    warnings.add("pdf_layout_uncertain")
                if item.metadata.get("pdf_ocr"):
                    warnings.add("pdf_ocr_requires_review")
                result.append(
                    replace(
                        item,
                        warnings=tuple(sorted(warnings)),
                        metadata={
                            **item.metadata,
                            "pdf_layout": name,
                            "pdf_band": band,
                            "pdf_column": column,
                        },
                    )
                )
    return result


def pdf_continuation(previous: ParsedBlock, current: ParsedBlock, in_section: bool) -> bool:
    if previous.kind != "text" or current.kind != "text":
        return False
    a, b = previous.locator.page, current.locator.page
    if a is None or b is None or b not in (a, a + 1):
        return False
    if (
        current.metadata.get("pdf_continues_prev") is True
        and current.metadata["pdf_previous_index"] == previous.metadata["pdf_original_index"]
    ):
        return True
    left_to_right = (
        a == b
        and previous.metadata["pdf_band"] == current.metadata["pdf_band"]
        and previous.metadata["pdf_column"] == "left"
        and current.metadata["pdf_column"] == "right"
    )
    known_next_page = (
        a != b
        and in_section
        and all(block.metadata["pdf_layout"] != "unknown" for block in (previous, current))
    )
    next_char = current.text.lstrip()[0]
    return (left_to_right or known_next_page) and (
        previous.text.rstrip()[-1] not in _SENTENCE_END
        and (next_char.islower() or "\u3400" <= next_char <= "\u9fff")
    )
