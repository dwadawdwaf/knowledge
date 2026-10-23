from __future__ import annotations

import pytest

from knowledge.domain.entities import (
    Evidence,
    GeneratedAnswer,
    GeneratedSection,
)
from knowledge.domain.errors import CitationValidationFailed
from knowledge.services.qa import _assemble


def _evidence(key: str = "E1") -> Evidence:
    return Evidence(
        key=key,
        chunk_id=f"chunk-{key}",
        document_id="doc-1",
        version_id="ver-1",
        build_id="build-1",
        title="会议室管理办法",
        text="使用后应清理现场，并关闭设备。",
        locator={"type": "pdf", "page": 2, "precision": "block"},
    )


def test_single_citation_numbered():
    answer, citations, warnings = _assemble(
        GeneratedAnswer(sections=(GeneratedSection("应清理现场。", ("E1",)),)),
        [_evidence("E1")],
    )
    assert answer == "应清理现场。[1]"
    assert len(citations) == 1
    assert citations[0].index == 1
    assert citations[0].excerpt.startswith("使用后应清理")
    assert warnings == []


def test_unknown_key_dropped_and_warned():
    answer, citations, warnings = _assemble(
        GeneratedAnswer(sections=(GeneratedSection("应清理现场。", ("E9", "E1")),)),
        [_evidence("E1")],
    )
    assert "[1]" in answer
    assert len(citations) == 1
    assert any("不存在" in warning for warning in warnings)


def test_no_valid_citation_rejected():
    with pytest.raises(CitationValidationFailed):
        _assemble(
            GeneratedAnswer(sections=(GeneratedSection("凭空结论。", ("E9",)),)),
            [_evidence("E1")],
        )


def test_section_without_keys_kept_but_warned():
    answer, citations, warnings = _assemble(
        GeneratedAnswer(
            sections=(
                GeneratedSection("应清理现场。", ("E1",)),
                GeneratedSection("另需登记。", ()),
            )
        ),
        [_evidence("E1")],
    )
    assert "另需登记。" in answer
    assert len(citations) == 1
    assert any("未标注证据" in warning for warning in warnings)


def test_same_key_reused_keeps_one_citation():
    answer, citations, _ = _assemble(
        GeneratedAnswer(
            sections=(
                GeneratedSection("第一句。", ("E1",)),
                GeneratedSection("第二句。", ("E1",)),
            )
        ),
        [_evidence("E1")],
    )
    assert len(citations) == 1
    assert answer.count("[1]") == 2
