"""自制材料覆盖自然边界、预算与来源定位，不依赖真实模型。"""

import pytest

from knowledge.domain.chunking import chunk_blocks
from knowledge.domain.entities import Locator, ParsedBlock


def block(text, ordinal, *, media="pdf", page=1, kind="text", level=None, box=None):
    return ParsedBlock(
        text, Locator(media, 0, len(text), "block", page=page if media == "pdf" else None, block=ordinal),
        kind=kind, heading_level=level, metadata={"pdf_bbox": box},
    )


@pytest.mark.parametrize("media", ["pdf", "doc", "docx", "md"])
def test_heading_paragraphs_and_lists_form_an_evidence_unit(media):
    source = [
        block("样品验收", 0, media=media, kind="heading", level=2),
        block("样品先称重，后编号。", 1, media=media),
        block("如有异常，按以下步骤处理：", 2, media=media),
        block("（一）核对记录。", 3, media=media, kind="heading", level=3),
        block("（二）封存样品。", 4, media=media),
        block("设备校准", 5, media=media, kind="heading", level=2),
        block("校准周期为六个月。", 6, media=media),
    ]
    chunks = chunk_blocks(source)
    assert [chunk.text for chunk in chunks] == [
        "\n".join(item.text for item in source[:5]), "\n".join(item.text for item in source[5:]),
    ]
    assert [chunk.structure["section_path"] for chunk in chunks] == [["样品验收"], ["设备校准"]]


def test_packing_respects_complete_items_and_splits_overlong_elements_at_sentences():
    heading = block("准备工作", 0, kind="heading", media="docx")
    first = block("- Connect the dry line.", 1, media="docx")
    second = block("- Mark the valve position.", 2, media="docx")
    prose = block("Keep seals clean. Record leaks immediately. Check the inlet.", 3, media="docx")
    chunks = chunk_blocks([heading, first, second, prose], max_chars=34, overlap=12)
    assert chunks[0].text == heading.text + "\n" + first.text
    assert chunks[1].text == second.text
    assert [p.text.strip() for p in chunks[2:]] == [
        "Keep seals clean.", "Record leaks immediately.", "Check the inlet.",
    ]
    assert {part.structure["unit_id"] for part in chunks} == {"0"}
    assert all(len(part.text) <= 34 and "准备工作" in part.retrieval_text for part in chunks)
    lookup = {p.locator.block: p.text for p in [heading, first, second, prose]}
    for part in chunks:
        for source in part.locator["sources"]:
            loc = source["locator"]
            assert part.text[source["chunk_start"]:source["chunk_end"]] == lookup[loc["block"]][
                loc["char_start"]:loc["char_end"]
            ]


@pytest.mark.parametrize("marker", ["-", r"\-"])
def test_unknown_pdf_page_holds_explicit_list_but_not_unrelated_page_or_table(marker):
    source = [
        block("安装前准备：", 0), block(f"{marker} 固定支架。", 1), block(f"{marker} 检查电源。", 2),
        block("设备\t数量\n阀门\t3", 3, kind="table"),
        block("另一页的文字。", 4, page=2),
    ]
    chunks = chunk_blocks(source)
    assert [part.text for part in chunks] == [
        "\n".join(part.text for part in source[:3]), source[3].text, source[4].text,
    ]
    assert "pdf_layout_uncertain" in chunks[0].warnings
    assert chunks[0].locator["pages"] == [1]
    assert chunks[1].structure["kind"] == "table"


def test_preserves_markdown_line_breaks_and_separates_sibling_sections():
    text = "## 校准\r\n\r\n先通电。\r\n- 等待读数。\r\n## 维护\r\n断开电源。"
    chunks = chunk_blocks([block(text, 0, media="md")])
    assert len(chunks) == 2 and "".join(part.text for part in chunks) == text
    assert [part.structure["section_path"] for part in chunks] == [["校准"], ["维护"]]
