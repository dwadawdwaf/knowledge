"""覆盖顺序、原始范围和补全关系的最小回归。"""

from dataclasses import replace

from knowledge.domain.entities import Locator, SearchHit
from knowledge.domain.evidence import coverage_order, structured_neighbors
from knowledge.domain.querying import evidence_requirements


def hit(key, text, *, locator=None, structure=None, anchor=None):
    return SearchHit(
        document_id="doc", kb_id="kb", kb_name="库", version_id="version", build_id="build",
        chunk_id=key, title="夹具", text=text, locator=locator or {}, score=1.0,
        structure=structure or {}, diagnostics={"neighbor_of": anchor} if anchor else {},
    )


def test_only_explicit_requirements_are_split_and_keep_their_scope():
    assert evidence_requirements("核对 ＨＢＲ－２７１ 的挂载点、正式上线时刻，以及夜间转接码。") == (
        "HBR-271 的 挂载点", "HBR-271 的 正式上线时刻", "HBR-271 的 夜间转接码",
    )
    assert evidence_requirements("谁负责本次正式签核？交接在哪个房间办理？") == (
        "谁负责本次正式签核", "交接在哪个房间办理",
    )
    assert evidence_requirements("核对 HBR-271 的挂载点、上线时刻，不要采用预演。") == ()
    assert evidence_requirements("HBR-271 的预算上限是多少？交接在哪个房间办理？") == ()
    assert evidence_requirements("HBR-271 的正式转接码是什么？") == ()


def test_coverage_keeps_first_result_and_deduplicates_shared_evidence():
    first, general, direct = (hit("a", "A"), hit("b", "B"), hit("c", "C"))
    output = coverage_order([first, general], [[first], [direct], [direct]])
    assert [item.chunk_id for item in output] == ["a", "c", "b"]
    assert output[1].diagnostics["evidence_requirements"] == [1, 2]
    assert coverage_order([], [[direct]]) == []
    assert coverage_order([first, general], [[]]) == [first, general]


def test_context_requires_matching_header_source_and_active_build():
    table = {"kind": "table_row", "table_id": "current"}
    locator = Locator("xlsx", 0, 10, "sheet", sheet="current", row_start=1, row_end=1).to_dict()
    anchor = hit("row", "XYZ 10", structure={**table, "context_sources": [locator]})
    header = hit(
        "header", "设备 值(毫秒)", locator=locator,
        structure={**table, "is_header": True}, anchor=anchor.chunk_id,
    )
    wrong_location = replace(header, chunk_id="wrong-location", locator={**locator, "row_start": 2})
    wrong_table = replace(header, chunk_id="wrong-table", structure={**header.structure, "table_id": "draft"})
    obsolete = replace(header, chunk_id="obsolete", build_id="previous")
    assert structured_neighbors([anchor], [wrong_location, obsolete, header, wrong_table, header]) == [header]
    assert structured_neighbors([anchor, header], [header]) == []


def test_context_joins_only_verifiable_continuation():
    structure = {"kind": "text", "unit_id": "same", "section_path": ["Policy"]}

    def locator(page):
        return Locator("pdf", 0, 18, "block", page=page, block=0).to_dict()

    anchor = hit("a", "Send the report to", locator=locator(1), structure=structure)
    continuation = hit("b", "the night duty officer.", locator=locator(2), structure=structure, anchor="a")
    another_section = replace(continuation, chunk_id="c", structure={**structure, "section_path": ["Other"]})
    nonadjacent = replace(continuation, chunk_id="d", locator=locator(3))
    upper = replace(continuation, chunk_id="e", text="The night duty officer.")
    assert structured_neighbors([anchor], [another_section, nonadjacent, continuation, upper]) == [continuation]
    assert structured_neighbors([replace(anchor, text="This concludes the policy.")], [continuation]) == []
