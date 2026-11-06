"""双栏、混排和保守退化的自制版面，验证分块及各来源区间。"""

import pytest

from knowledge.domain.chunking import chunk_blocks
from knowledge.domain.entities import Locator, ParsedBlock

STRATEGY = "pdf-layout-v1"


def block(text, index, box=None, *, page=1, kind="text", **metadata):
    return ParsedBlock(
        text,
        Locator("pdf", 0, len(text), "page", page=page, block=index),
        kind=kind,
        metadata={"pdf_bbox": box, **metadata},
    )


def test_two_columns_restore_reading_order_and_join_only_continuing_paragraph():
    blocks = [
        block("Left first paragraph.", 0, (0.08, 0.10, 0.45, 0.22)),
        block("at the fixed threshold.", 1, (0.54, 0.12, 0.92, 0.21)),
        block("The measurement ends", 2, (0.08, 0.24, 0.45, 0.38)),
        block("Right last paragraph.", 3, (0.54, 0.30, 0.92, 0.45)),
    ]
    old = chunk_blocks(blocks, strategy="legacy-v1")
    assert [part.text for part in old] == [part.text for part in blocks]
    parts = chunk_blocks(blocks, strategy=STRATEGY)
    assert [part.text for part in parts] == [
        "Left first paragraph.",
        "The measurement ends\nat the fixed threshold.",
        "Right last paragraph.",
    ]
    joined = parts[1]
    assert joined.structure["pdf_layout"] == "two_column"
    assert "cross_column_continuation_candidate" in joined.warnings
    assert joined.locator["pages"] == [1]
    assert [source["locator"]["pdf_column"] for source in joined.locator["sources"]] == [
        "left",
        "right",
    ]
    for source, original in zip(joined.locator["sources"], (blocks[2], blocks[1]), strict=True):
        assert joined.text[source["chunk_start"] : source["chunk_end"]] == original.text
        assert source["locator"]["block"] == original.locator.block


def test_mixed_layout_orders_each_band_and_preserves_table_and_caption():
    parts = chunk_blocks(
        [
            block("Wide abstract.", 0, (0.08, 0.04, 0.92, 0.10)),
            block("Top right.", 1, (0.54, 0.17, 0.92, 0.32)),
            block("Top left.", 2, (0.08, 0.15, 0.45, 0.33)),
            block(
                "",
                3,
                (0.08, 0.38, 0.92, 0.51),
                kind="figure",
                pdf_captions=[
                    {"content": "Figure 1. Calibration curve.", "bbox": (0.08, 0.51, 0.9, 0.55)},
                ],
            ),
            block("Bottom right.", 4, (0.54, 0.61, 0.92, 0.76)),
            block("Bottom left.", 5, (0.08, 0.60, 0.45, 0.78)),
            block("Metric\tValue\nAccuracy\t91%", 6, (0.08, 0.81, 0.92, 0.95), kind="table"),
        ],
        strategy=STRATEGY,
    )
    assert [part.text for part in parts] == [
        "Wide abstract.",
        "Top left.",
        "Top right.",
        "Figure 1. Calibration curve.",
        "Bottom left.",
        "Bottom right.",
        "Metric\tValue\nAccuracy\t91%",
    ]
    assert all(part.structure["pdf_layout"] == "mixed" for part in parts)
    assert parts[3].locator["pdf_component"] == "caption:0"
    assert parts[-1].structure["kind"] == "table"


def test_single_column_continuation_preserves_pages_and_stops_at_heading():
    parts = chunk_blocks(
        [
            block("1 Methods", 0, (0.09, 0.1, 0.7, 0.15), kind="heading"),
            block("The reagent was kept", 1, (0.09, 0.7, 0.85, 0.9)),
            block("under nitrogen.", 2, (0.09, 0.1, 0.80, 0.25), page=2),
            block("2 Results", 3, (0.09, 0.4, 0.6, 0.45), page=2, kind="heading"),
            block("Fresh samples were used.", 4, (0.09, 0.5, 0.85, 0.7), page=2),
        ],
        strategy=STRATEGY,
    )
    assert parts[1].text == "The reagent was kept\nunder nitrogen."
    assert parts[1].locator["pages"] == [1, 2]
    assert "cross_page_continuation_candidate" in parts[1].warnings
    assert parts[-1].structure["section_path"] == ["2 Results"]


@pytest.mark.parametrize(
    "boxes",
    [
        [None, (0.50, 0.12, 0.62, 0.50), (0.70, 0.11, 0.90, 0.52)],
        [(0.09, 0.10, 0.30, 0.52), (0.38, 0.12, 0.62, 0.50), (0.70, 0.11, 0.90, 0.52)],
    ],
)
def test_missing_or_ambiguous_geometry_keeps_original_order_and_warns(boxes):
    blocks = [
        block(f"Paragraph {index}", index, box, pdf_ocr=True) for index, box in enumerate(boxes)
    ]
    parts = chunk_blocks(blocks, strategy=STRATEGY)
    assert [part.text for part in parts] == [part.text for part in blocks]
    assert all(part.structure["pdf_layout"] == "unknown" for part in parts)
    assert all(
        {"pdf_layout_uncertain", "pdf_ocr_requires_review"} <= set(part.warnings) for part in parts
    )


def test_equation_is_independent_and_long_prose_prefers_sentence_boundaries():
    prose = "First complete sentence. Second complete sentence. Third complete sentence."
    parts = chunk_blocks(
        [
            block(prose, 0, (0.08, 0.10, 0.92, 0.32)),
            block("E = mc^2", 1, (0.38, 0.34, 0.55, 0.38), kind="equation"),
        ],
        max_chars=32,
        overlap=0,
        strategy=STRATEGY,
    )
    assert [part.text.strip() for part in parts] == [
        "First complete sentence.",
        "Second complete sentence.",
        "Third complete sentence.",
        "E = mc^2",
    ]
    assert "".join(part.text for part in parts[:-1]) == prose
    assert parts[-1].structure["kind"] == "equation"


def test_pdf_table_uses_row_boundaries_without_repeating_partial_rows():
    table = "Material\tAmount\nCatalyst\t72 mg\nSolvent\t86 ml"
    parts = chunk_blocks(
        [block(table, 0, kind="table")],
        max_chars=23,
        overlap=5,
        strategy=STRATEGY,
    )
    assert [part.text.strip() for part in parts] == table.splitlines()
    assert "".join(part.text for part in parts) == table
