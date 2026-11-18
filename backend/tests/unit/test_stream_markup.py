"""流式答案的标记协议与引用校验（不连接数据库、不调用模型）。"""

from __future__ import annotations

import pytest

from knowledge.domain.citations import MarkerStream, marker_keys, strip_markers
from knowledge.domain.entities import Evidence
from knowledge.domain.errors import CitationValidationFailed
from knowledge.services.qa import assemble_marked


def _evidence(*keys: str) -> list[Evidence]:
    return [
        Evidence(
            key=key,
            chunk_id=f"chunk-{key}",
            document_id="doc",
            version_id="version",
            build_id="build",
            title=f"资料{key}",
            text="正文",
            locator={},
        )
        for key in keys
    ]


def test_marker_keys_preserve_first_appearance_order():
    assert marker_keys("a[E2] b[E1] c[E2]") == ("E2", "E1")


def test_strip_markers_removes_all_annotations():
    assert strip_markers("结论[E1]。另一点[E2]。") == "结论。另一点。"


def test_marker_stream_holds_back_partial_marker():
    stream = MarkerStream()
    assert stream.feed("结论[") == "结论"
    assert stream.feed("E") == ""
    assert stream.feed("1] 继续") == " 继续"
    assert stream.flush() == ""


def test_marker_stream_emits_lone_bracket_on_flush():
    stream = MarkerStream()
    assert stream.feed("无标记正文[") == "无标记正文"
    assert stream.flush() == "["


def test_assemble_marked_numbers_citations_by_first_appearance():
    body, citations, warnings = assemble_marked(
        "第一段[E2]。\n\n第二段[E1][E2]。", _evidence("E1", "E2")
    )
    assert body == "第一段。\n\n第二段。"
    assert [item.index for item in citations] == [1, 2]
    assert [item.chunk_id for item in citations] == ["chunk-E2", "chunk-E1"]
    assert citations[0].excerpt == "正文"
    assert warnings == []


def test_unknown_keys_are_dropped_with_warning():
    body, citations, warnings = assemble_marked("有效[E1]，无效[E9]。", _evidence("E1"))
    assert body == "有效，无效。"
    assert [item.chunk_id for item in citations] == ["chunk-E1"]
    assert warnings == ["已丢弃 1 个不存在的证据键"]


def test_paragraph_without_marker_is_kept_but_warned():
    _body, citations, warnings = assemble_marked(
        "有依据[E1]。\n\n没有标注的一段。", _evidence("E1")
    )
    assert len(citations) == 1
    assert warnings == ["存在未标注证据的段落"]


def test_answer_without_any_valid_citation_fails():
    with pytest.raises(CitationValidationFailed):
        assemble_marked("没有任何标记的答案", _evidence("E1"))
    with pytest.raises(CitationValidationFailed):
        assemble_marked("只有未知键[E7]", _evidence("E1"))
    with pytest.raises(CitationValidationFailed):
        assemble_marked("[E1]", _evidence("E1"))
