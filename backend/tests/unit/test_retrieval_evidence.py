"""问答只使用直接候选，验证切换后条数、字符和重复边界。"""

from dataclasses import replace

from knowledge.domain.entities import SearchHit
from knowledge.services.retrieval import select_evidence


def test_direct_evidence_keeps_rank_and_returns_existing_items_when_budget_is_full(settings, actor):
    configured = settings.model_copy(update={"evidence_max_chars": 800, "evidence_max_items": 3})
    first = SearchHit(
        document_id="doc-a", kb_id="kb-a", kb_name="fixture", version_id="v-a", build_id="b-a",
        chunk_id="one", title="fixture", text="甲" * 500, locator={}, score=0.9,
    )
    oversized = replace(first, chunk_id="large", text="大" * 900)
    duplicate = replace(first, chunk_id="duplicate")
    contains_first = replace(first, chunk_id="contains", text=first.text + "补充" * 10)
    second = replace(first, document_id="doc-b", chunk_id="two", text="乙" * 300)
    extra = replace(first, document_id="doc-c", chunk_id="three", text="丙")
    hits = [oversized, first, duplicate, contains_first, second, extra]
    selected = select_evidence(None, actor, configured, [], hits)
    assert [hit.chunk_id for hit in selected] == ["one", "two"]
    assert sum(len(hit.text) for hit in selected) == 800
    assert all(hit in hits for hit in selected)

