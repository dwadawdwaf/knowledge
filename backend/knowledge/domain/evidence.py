"""按需求排列直接证据，并校验可补充的原文关系。"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from typing import Any

from knowledge.domain.entities import SearchHit


def coverage_order(
    original: list[SearchHit], groups: Sequence[Sequence[SearchHit]],
) -> list[SearchHit]:
    if not original:
        return []
    direct = {hit.chunk_id: hit for hit in original}
    selected = {original[0].chunk_id: original[0]}
    covered: dict[str, list[int]] = {}
    for index, hits in enumerate(groups):
        if not hits:
            continue
        hit = hits[0]
        previous = next((
            item for item in selected.values()
            if item.chunk_id == hit.chunk_id
            or (item.document_id == hit.document_id and item.text == hit.text)
        ), None)
        if previous is not None:
            covered.setdefault(previous.chunk_id, []).append(index)
        else:
            selected[hit.chunk_id] = direct.get(hit.chunk_id, hit)
            covered[hit.chunk_id] = [index]
    if not covered:
        return original
    return [
        replace(hit, diagnostics={
            **hit.diagnostics, "coverage_priority": True,
            "evidence_requirements": covered.get(hit.chunk_id, []),
        })
        for hit in selected.values()
    ] + [hit for hit in original if hit.chunk_id not in selected]


def _source_locators(hit: SearchHit) -> list[dict[str, Any]]:
    return [source["locator"] for source in hit.locator.get("sources", [])] or [hit.locator]


def _context_priority(anchor: SearchHit, candidate: SearchHit) -> int | None:
    if (anchor.document_id, anchor.build_id) != (candidate.document_id, candidate.build_id):
        return None
    a, b = anchor.structure, candidate.structure
    a_locs, b_locs = _source_locators(anchor), _source_locators(candidate)
    if (
        a.get("kind") == "table_row" and not a.get("is_header")
        and a.get("table_id") and a["table_id"] == b.get("table_id")
        and b.get("is_header") and any(
            all(locator.get(key) == value for key, value in source.items())
            for source in a.get("context_sources", []) if source
            for locator in b_locs
        )
    ):
        return 0
    if (
        a.get("kind") != "text" or b.get("kind") != "text"
        or not a.get("unit_id") or a["unit_id"] != b.get("unit_id")
        or a.get("section_path") != b.get("section_path")
    ):
        return None

    for left_hit, right_hit, left, right in (
        (anchor, candidate, a_locs, b_locs), (candidate, anchor, b_locs, a_locs),
    ):
        tail, head = left[-1], right[0]
        if tail.get("type") != head.get("type"):
            continue
        left_text, right_text = left_hit.text.rstrip(), right_hit.text.lstrip()
        if not left_text or not right_text:
            continue
        page = tail.get("page")
        if (
            tail.get("type") == "pdf" and a.get("section_path")
            and isinstance(page, int) and head.get("page") == page + 1
            and not left_text.endswith(("。", "！", "？", ".", "!", "?", "：", ":"))
            and (right_text[0].islower() or "\u3400" <= right_text[0] <= "\u9fff")
        ):
            return 1
        # 同页只连接同一个原始块的连续窗口，避免把相邻但独立的段落加入证据。
        identified = tail.get("block") is not None or tail.get("paragraph") is not None
        if (
            identified and all(tail.get(key) == head.get(key)
                               for key in ("page", "block", "paragraph", "sheet"))
            and tail["char_start"] < head["char_start"] <= tail["char_end"]
        ):
            return 2
    return None


def structured_neighbors(
    anchors: Sequence[SearchHit], candidates: Sequence[SearchHit],
) -> list[SearchHit]:
    """只在仓库已返回的授权候选中挑选可核验的关系。"""
    by_id = {hit.chunk_id: (index, hit) for index, hit in enumerate(anchors)}
    ranked = []
    for candidate in candidates:
        anchor_id = candidate.diagnostics.get("neighbor_of")
        parent = by_id.get(anchor_id) if isinstance(anchor_id, str) else None
        if parent is None or candidate.chunk_id in by_id:
            continue
        index, anchor = parent
        priority = _context_priority(anchor, candidate)
        if priority is not None:
            ranked.append((priority, index, candidate.chunk_id, candidate))
    distinct: dict[str, SearchHit] = {}
    for _, _, chunk_id, hit in sorted(ranked, key=lambda item: item[:3]):
        distinct.setdefault(chunk_id, hit)
    return list(distinct.values())
