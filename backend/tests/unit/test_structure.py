"""结构边界、跨页来源与表格行号的自制回归。"""

import io
from dataclasses import replace

import pytest
from docx import Document

from knowledge.adapters.parser import CsvParser, DocxParser, TextParser
from knowledge.domain.chunking import chunk_blocks
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.querying import plan_query


def block(text: str, page: int, index: int = 0) -> ParsedBlock:
    return ParsedBlock(
        text, Locator("pdf", 0, len(text), page=page, block=index, precision="block")
    )


def test_cross_page_continuation_keeps_every_original_span():
    blocks = [block("# 报销规则", 1), block("未经批准的", 1, 1), block("超标准费用由个人承担。", 2)]
    pieces = chunk_blocks(blocks)
    assert len(pieces) == 1
    piece = pieces[0]
    assert piece.locator["pages"] == [1, 2]
    assert piece.locator["precision"] == "composite"
    assert "char_start" not in piece.locator
    lookup = {(b.locator.page, b.locator.block): b.text for b in blocks}
    for source in piece.locator["sources"]:
        loc = source["locator"]
        expected = lookup[(loc["page"], loc["block"])][loc["char_start"] : loc["char_end"]]
        assert piece.text[source["chunk_start"] : source["chunk_end"]] == expected


@pytest.mark.parametrize("newline", ["\n", "\r\n"])
def test_paragraphs_recognize_line_endings_without_changing_offsets(newline):
    text = (
        f"# 规则{newline}{newline}第一段第一行{newline}第一段第二行。{newline} \t{newline}第二段。"
    )
    blocks = TextParser().parse(text.encode(), "text/markdown", "synthetic.md")
    assert len(blocks) == 3
    assert newline in blocks[1].text
    for item in blocks:
        assert text[item.locator.char_start : item.locator.char_end] == item.text


def test_unknown_cross_page_and_new_sections_stay_separate():
    pieces = chunk_blocks([block("上一页未完", 1), block("下一页内容", 2)])
    assert len(pieces) == 2
    pieces = chunk_blocks(
        [block("# 第一节\n旧内容未完", 1), block("# 第二节\n新内容。", 2)]
    )
    assert len(pieces) == 2
    assert all(not ("旧内容" in p.text and "新内容" in p.text) for p in pieces)


def test_only_explicit_running_matter_is_omitted_with_warning():
    blocks = [replace(block("文件页眉", 1), kind="header"), block("正文中的文件页眉也须保留。", 1, 1)]
    pieces = chunk_blocks(blocks)
    assert len(pieces) == 1 and pieces[0].text == blocks[1].text
    assert "running_matter_omitted" in pieces[0].warnings


def test_sparse_table_row_is_not_dropped_when_trailing_cells_are_empty():
    blocks = CsvParser().parse("设备,位置\nEQ-NX-803,\n".encode(), "text/csv", "synthetic.csv")
    pieces = chunk_blocks(blocks)
    row = next(piece for piece in pieces if "EQ-NX-803" in piece.text)
    assert row.locator["row_start"] == 2


def test_long_element_slices_preserve_sources_and_article_parent():
    text = "第十二条 费用管理\n" + "超过标准须批准。" * 40
    pieces = chunk_blocks([block(text, 1)], max_chars=90, overlap=10)
    assert len(pieces) > 2
    assert len({p.structure["unit_id"] for p in pieces}) == 1
    for piece in pieces:
        assert len(piece.text) <= 90
        for source in piece.locator["sources"]:
            loc = source["locator"]
            assert (
                piece.text[source["chunk_start"] : source["chunk_end"]]
                == text[loc["char_start"] : loc["char_end"]]
            )


def test_table_header_crosses_batches_but_keeps_real_row_numbers():
    blocks = CsvParser(rows_per_block=2).parse(
        "资产\t位置\n".replace("\t", ",").encode() + b"A-01,301\n\nB-02,502\n",
        "text/csv",
        "synthetic.csv",
    )
    pieces = chunk_blocks(blocks)
    row = next(piece for piece in pieces if "B-02" in piece.text)
    assert row.locator["row_start"] == row.locator["row_end"] == 4
    assert "位置" in row.retrieval_text
    assert "位置" not in row.text
    assert row.structure["context_sources"][0]["row_start"] == 1
    assert "table_header_candidate" in row.warnings


def test_docx_structure_restores_interleaved_table_order():
    document = Document()
    document.add_paragraph("表格之前。")
    table = document.add_table(rows=2, cols=2)
    table.cell(0, 0).text, table.cell(0, 1).text = "设备", "位置"
    table.cell(1, 0).text, table.cell(1, 1).text = "A-01", "301"
    document.add_paragraph("表格之后。")
    output = io.BytesIO()
    document.save(output)
    blocks = DocxParser().parse(
        output.getvalue(),
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "synthetic.docx",
    )
    structured = chunk_blocks(blocks)
    joined = "\n".join(piece.text for piece in structured)
    assert joined.index("表格之前") < joined.index("A-01") < joined.index("表格之后")


@pytest.mark.parametrize(
    "query", ["住宿不得超过３５０元，例外是什么？", "第十二条不适用哪些情况", "EQ-NX-803在什么位置"]
)
def test_query_plan_preserves_constraints_and_original(query):
    plan = plan_query(query)
    assert plan.variants[0] == query
    assert len(plan.variants) <= 3
    if "不" in query:
        assert all("不" in item for item in plan.variants)


def test_query_identifiers_and_explicit_subquestions_are_bounded():
    assert plan_query("第十二条与EQ-NX-803").exact_terms == ("第十二条", "EQ-NX-803")
    plan = plan_query("设备如何办理借用；损坏之后如何赔偿", 2)
    assert len(plan.variants) == 2
    assert plan.intent == "multi"
