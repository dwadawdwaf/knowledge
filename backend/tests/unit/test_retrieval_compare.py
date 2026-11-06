"""新增来源指标独立于历史匹配，不能用其他页的文字充当金标。"""

from scripts.retrieval_baseline import Fact, supports_fact
from scripts.retrieval_compare import provenance_supports


def test_composite_fact_only_matches_page_containing_its_text():
    locator = {
        "page": 12,
        "precision": "composite",
        "sources": [
            {"chunk_start": 0, "chunk_end": 3, "locator": {"page": 12}},
            {"chunk_start": 4, "chunk_end": 8, "locator": {"page": 13}},
        ],
    }
    text = "申请人\n负责费用"
    fact = Fact(document="synthetic", quote="负责费用", aliases=["费用"], pdf_pages=[13])
    assert not supports_fact(fact, text, locator)
    assert provenance_supports(fact, text, locator)
    fact.pdf_pages = [12]
    assert not provenance_supports(fact, text, locator)


def test_normalized_match_checks_its_actual_source_range():
    text = "背景。\nＡI 费用"
    locator = {
        "sources": [
            {"chunk_start": 0, "chunk_end": 3, "locator": {"page": 1}},
            {"chunk_start": 4, "chunk_end": len(text), "locator": {"page": 2}},
        ]
    }
    fact = Fact(
        document="synthetic", quote="ai费用", aliases=["费用"], normalized=True, pdf_pages=[2]
    )
    assert provenance_supports(fact, text, locator)
    fact.pdf_pages = [1]
    assert not provenance_supports(fact, text, locator)
