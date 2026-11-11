"""PDF 阅读顺序、独立元素和来源区间在新分块流程中的回归。"""

import pytest

from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.pdf_layout import order_pdf_blocks
from knowledge.domain.ragflow_chunking import chunk_ragflow


def block(text, index, box=None, *, page=1, kind="text", **metadata):
    return ParsedBlock(
        text,
        Locator("pdf", 0, len(text), "page", page=page, block=index),
        kind=kind,
        metadata={"pdf_bbox": box, **metadata},
    )


def test_two_columns_restore_reading_order_and_keep_original_sources():
    blocks = [
        block("Left first paragraph.", 0, (0.08, 0.10, 0.45, 0.22)),
        block("at the fixed threshold.", 1, (0.54, 0.12, 0.92, 0.21)),
        block("The measurement ends", 2, (0.08, 0.24, 0.45, 0.38)),
        block("Right last paragraph.", 3, (0.54, 0.30, 0.92, 0.45)),
    ]
    ordered = order_pdf_blocks(blocks)
    assert [item.locator.block for item in ordered] == [0, 2, 1, 3]
    parts = chunk_ragflow(blocks, len, mineru_pdf=True)
    sources = [source for part in parts for source in part.locator["sources"]]
    assert [source["locator"]["pdf_column"] for source in sources] == ["left", "left", "right", "right"]
    for part in parts:
        for source in part.locator["sources"]:
            loc = source["locator"]
            assert part.text[source["chunk_start"]:source["chunk_end"]] == blocks[loc["block"]].text


def test_mixed_layout_preserves_caption_and_table_rows_once():
    blocks = [
        block("Wide abstract.", 0, (0.08, 0.04, 0.92, 0.10)),
        block("Top right.", 1, (0.54, 0.17, 0.92, 0.32)),
        block("Top left.", 2, (0.08, 0.15, 0.45, 0.33)),
        block("", 3, (0.08, 0.38, 0.92, 0.51), kind="figure", pdf_captions=[
            {"content": "Figure 1. Calibration curve.", "bbox": (0.08, 0.51, 0.9, 0.55)},
        ]),
        block("Bottom right.", 4, (0.54, 0.61, 0.92, 0.76)),
        block("Bottom left.", 5, (0.08, 0.60, 0.45, 0.78)),
        block("Metric\tValue\nAccuracy\t91%", 6, (0.08, 0.81, 0.92, 0.95), kind="table"),
    ]
    parts = chunk_ragflow(blocks, len, mineru_pdf=True)
    joined = "\n".join(part.text for part in parts)
    assert joined.index("Top left") < joined.index("Top right") < joined.index("Calibration")
    assert joined.index("Bottom left") < joined.index("Bottom right")
    assert joined.count("Calibration curve.") == joined.count("Accuracy\t91%") == 1
    assert [part.text for part in parts if part.structure["kind"] == "table_row"] == [
        "Metric\tValue", "Accuracy\t91%",
    ]


@pytest.mark.parametrize("boxes", [
    [None, (0.50, 0.12, 0.62, 0.50), (0.70, 0.11, 0.90, 0.52)],
    [(0.09, 0.10, 0.30, 0.52), (0.38, 0.12, 0.62, 0.50), (0.70, 0.11, 0.90, 0.52)],
])
def test_missing_or_ambiguous_geometry_keeps_original_order_and_warns(boxes):
    blocks = [block(f"Paragraph {index}", index, box, pdf_ocr=True) for index, box in enumerate(boxes)]
    ordered = order_pdf_blocks(blocks)
    assert [item.text for item in ordered] == [item.text for item in blocks]
    assert all(item.metadata["pdf_layout"] == "unknown" for item in ordered)
    assert all({"pdf_layout_uncertain", "pdf_ocr_requires_review"} <= set(item.warnings) for item in ordered)


def test_equation_remains_independent_and_source_slices_preserve_prose():
    prose = "First complete sentence. Second complete sentence. Third complete sentence." * 20
    parts = chunk_ragflow([
        block(prose, 0, (0.08, 0.10, 0.92, 0.32)),
        block("E = mc^2", 1, (0.38, 0.34, 0.55, 0.38), kind="equation"),
    ], len, mineru_pdf=True)
    assert "".join(part.text for part in parts[:-1]) == prose
    assert parts[-1].text == "E = mc^2" and parts[-1].structure["kind"] == "equation"
