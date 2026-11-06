"""优化路径的真实数据库授权、关联证据与冻结配置回归；全部自制文本。"""

import io
import json
import os
import uuid
import zipfile
from dataclasses import replace

import pytest
from fpdf import FPDF
from sqlalchemy import text

from knowledge.adapters.generator import StubGenerator
from knowledge.domain.entities import Actor
from knowledge.domain.errors import ModelUnavailable, NotFound, ValidationFailed
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories.tables import IndexBuild
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import qa, retrieval
from knowledge.worker.loop import process_next

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated database required"),
]


def test_real_two_page_pdf_preserves_composite_citation(session, settings, deps, actor):
    configured = settings.model_copy(
        update={"chunk_strategy": "structure-v2", "retrieval_profile": "optimized-v2"}
    )
    deps.settings = configured
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.multi_cell(0, 10, "# Expense policy\nUnapproved excess")
    pdf.add_page()
    pdf.multi_cell(0, 10, "costs are paid personally.")
    kb = knowledge_service.create(session, actor, "跨页PDF自制资料")
    ack = document_service.upload(
        session,
        actor,
        configured,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="synthetic.pdf",
        data=bytes(pdf.output()),
        parser_profile="legacy-v1",
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    while process_next(session, deps):
        pass
    answer = qa.answer(
        session,
        actor,
        configured,
        deps.embedder,
        StubGenerator(),
        question="Unapproved excess costs",
        kb_ids=[kb.id],
    )
    assert answer.status == "answered"
    citation = answer.citations[0]
    assert citation.locator["pages"] == [1, 2]
    assert citation.locator["precision"] == "composite"
    assert "char_start" not in citation.locator
    assert citation.build_id == str(ack.build_id)
    assert {source["locator"]["page"] for source in citation.locator["sources"]} == {1, 2}
    with pytest.raises(NotFound):
        chunk_repo.get_owned(session, Actor("other", "default"), uuid.UUID(citation.chunk_id))


def test_pdf_layout_upload_reorders_columns_and_retrieves_cross_page_evidence(
    session, settings, deps, actor, monkeypatch,
):
    configured = settings.model_copy(update={
        "pdf_chunk_strategy": "pdf-layout-v1", "retrieval_profile": "optimized-v2",
    })
    deps.settings = configured
    # PDF 原件由真实写入器生成。解析服务响应使用独立的版面夹具，刻意按行交错两栏。
    pages = [
        [
            ("paragraph_title", "1 Results", [0.43, 0.05, 0.57, 0.10]),
            ("text", "Calibration summary.", [0.08, 0.16, 0.45, 0.20]),
            ("text", "ninety-one percent.", [0.55, 0.16, 0.92, 0.20]),
            ("text", "Catalyst yield is", [0.08, 0.35, 0.45, 0.39]),
            ("text", "The verification sample was", [0.55, 0.35, 0.92, 0.39]),
        ],
        [("text", "stored under nitrogen.", [0.08, 0.14, 0.92, 0.18])],
    ]
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    for blocks in pages:
        pdf.add_page()
        for _, value, box in blocks:
            pdf.text(pdf.w * box[0], pdf.h * box[3], value)
    producer = {"name": "mineru", "version": "4.1.0"}
    response = io.BytesIO()
    with zipfile.ZipFile(response, "w") as archive:
        archive.writestr("structured_content.json", json.dumps({
            "metadata": {"producer": producer}, "is_full_document": True,
            "pages": [{"page_idx": index, "blocks": [
                {"type": kind, "content": value} for kind, value, _ in blocks
            ]} for index, blocks in enumerate(pages)],
        }))
        archive.writestr("middle_json.json", json.dumps({
            "schema": "docvortex.middle", "schema_version": "2.0",
            "metadata": {"producer": producer}, "is_full_document": True,
            "pages": [{"page_idx": index, "blocks": [
                {"type": kind, "bbox": box} for kind, _, box in blocks
            ]} for index, blocks in enumerate(pages)],
        }))
    monkeypatch.setattr("knowledge.adapters.mineru._open", lambda *_args, **_kwargs: io.BytesIO(response.getvalue()))
    kb = knowledge_service.create(session, actor, "期刊双栏对照")
    ack = document_service.upload(
        session, actor, configured, deps.storage, deps.registry, kb_id=kb.id,
        filename="journal.pdf", data=bytes(pdf.output()), parser_profile="mineru-api4.1-v1",
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    while process_next(session, deps):
        pass
    build = session.get(IndexBuild, ack.build_id)
    assert build.state == "ready" and build.chunk_config["strategy"] == "pdf-layout-v1"
    hits = retrieval.search(
        session, actor, configured, deps.embedder, query="verification sample stored under nitrogen",
        kb_ids=[kb.id],
    )
    assert hits[0].text == "The verification sample was\nstored under nitrogen."
    assert hits[0].locator["pages"] == [1, 2]
    assert hits[0].structure["section_path"] == ["1 Results"]
    answer = qa.answer(
        session, actor, configured, deps.embedder, StubGenerator(),
        question="verification sample stored under nitrogen", kb_ids=[kb.id],
    )
    assert any(citation.locator.get("pages") == [1, 2] for citation in answer.citations)
    with pytest.raises(NotFound):
        chunk_repo.get_owned(session, Actor("other", "default"), uuid.UUID(hits[0].chunk_id))
    _, other = ingest(session, configured, deps, actor, "普通文档", "这是一份普通文档。")
    assert session.get(IndexBuild, other.build_id).chunk_config["strategy"] == configured.chunk_strategy


def ingest(session, settings, deps, actor, name, content):
    kb = knowledge_service.create(session, actor, name)
    result = document_service.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename=name + ".md",
        data=content.encode(),
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    while process_next(session, deps):
        pass
    assert session.get(IndexBuild, result.build_id).state == "ready"
    return kb, result


def test_optimized_exact_retrieval_keeps_diagnostics_and_scope(session, settings, deps, actor):
    optimized = settings.model_copy(update={"retrieval_profile": "optimized-v2"})
    kb, _ = ingest(
        session, settings, deps, actor, "资产", "# 资产台账\n\n设备EQ-NX-803存放于北楼302室。"
    )
    other = Actor("other", "default")
    ingest(session, settings, deps, other, "私有", "设备EQ-NX-803存放于南楼999室。")
    hits = retrieval.search(
        session, actor, optimized, deps.embedder, query="ＥＱ－ＮＸ－８０３在哪里", kb_ids=[kb.id]
    )
    assert hits and all(hit.kb_id == str(kb.id) for hit in hits)
    assert "北楼302" in hits[0].text
    assert hits[0].diagnostics["keyword_score"] == 1.0
    assert hits[0].diagnostics["keyword_rank"] is not None
    with pytest.raises(ValidationFailed):
        retrieval.search(
            session, actor, settings, deps.embedder, query="设备", kb_ids=[kb.id], top_k=0
        )


def test_fts_stems_terms_and_preserves_published_scope(session, settings, deps, actor, monkeypatch):
    baseline = settings.model_copy(update={"retrieval_profile": "optimized-v2", "query_variants_max": 1})
    fts = baseline.model_copy(update={
        "retrieval_profile": "fts-v1", "query_variants_max": 3,
        "evidence_coverage": True, "reranker_url": "http://127.0.0.1:1",
    })
    kb, original = ingest(
        session, settings, deps, actor, "Signals", "The telescope measures angular velocities. 台账 EQ-ZZ-821。",
    )
    other = Actor("fts-other", "default")
    other_kb, _ = ingest(
        session, settings, deps, other, "Private", "The private telescope measures angular velocities.",
    )
    calls = []
    search_chunks = chunk_repo.search

    def record(*args, **kwargs):
        calls.append((kwargs["query"], kwargs["full_text"]))
        return search_chunks(*args, **kwargs)

    monkeypatch.setattr(chunk_repo, "search", record)

    def search(query, profile=fts, ids=None, mode="keyword"):
        return retrieval.search(
            session, actor, profile, deps.embedder,
            query=query, kb_ids=ids or [kb.id], mode=mode,
        )

    hits = search("measuring; unrelated polar motion?", mode="hybrid")
    assert [hit.build_id for hit in hits] == [str(original.build_id)]
    assert hits[0].diagnostics["keyword_score"] > 0
    assert calls == [("measuring; unrelated polar motion?", True)]
    for query in ("台账", "EQ-ZZ-821", "the and"):
        assert search(query) == search(query, baseline)
    with pytest.raises(NotFound):
        search("telescope", ids=[other_kb.id])

    updated = document_service.add_version(
        session, actor, settings, deps.storage, deps.registry, document_id=original.document_id,
        filename="Signals.md", data=b"The radio antenna records solar pulses.", expected_generation=None,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    while process_next(session, deps):
        pass
    assert not search("telescope")
    assert [hit.build_id for hit in search("antennas")] == [str(updated.build_id)]
    document_service.delete(session, actor, original.document_id, settings.max_attempts)
    assert not search("antennas")


def test_structure_headers_related_are_authorized_and_blocked_after_delete(
    session, settings, deps, actor
):
    optimized = settings.model_copy(
        update={"chunk_strategy": "structure-v2", "retrieval_profile": "optimized-v2"}
    )
    deps.settings = optimized
    kb = knowledge_service.create(session, actor, "表格")
    result = document_service.upload(
        session,
        actor,
        optimized,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="台账.csv",
        data="设备,位置\nEQ-NX-803,北楼302\n".encode(),
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    while process_next(session, deps):
        pass
    hits = retrieval.search(
        session, actor, optimized, deps.embedder, query="EQ-NX-803", kb_ids=[kb.id]
    )
    hit = next(item for item in hits if "EQ-NX-803" in item.text)
    related = chunk_repo.related(session, actor, [kb.id], [hit], 2)
    assert related and related[0].structure["is_header"]
    assert all(item.build_id == hit.build_id for item in related)
    structured = optimized.model_copy(update={"structured_context": True})
    evidence = retrieval.select_evidence(session, actor, structured, [kb.id], [hit])
    assert [item.chunk_id for item in evidence] == [hit.chunk_id, related[0].chunk_id]
    too_small = structured.model_copy(update={"evidence_max_chars": len(hit.text)})
    assert retrieval.select_evidence(session, actor, too_small, [kb.id], [hit]) == [hit]
    assert chunk_repo.related(session, Actor("other", "default"), [kb.id], [hit], 2) == []
    unrelated = knowledge_service.create(session, actor, "其他范围")
    assert chunk_repo.related(session, actor, [unrelated.id], [hit], 2) == []
    session.execute(
        text("UPDATE documents SET status='deleting' WHERE id=:id"), {"id": result.document_id}
    )
    session.commit()
    assert chunk_repo.related(session, actor, [kb.id], [hit], 2) == []


def test_reranking_is_injected_and_failures_are_not_no_evidence(session, settings, deps, actor):
    kb, _ = ingest(
        session,
        settings,
        deps,
        actor,
        "会议",
        "# 预约\n\n会议室提前一天预约。\n\n# 清理\n\n会议室用后清理。",
    )
    optimized = settings.model_copy(
        update={"retrieval_profile": "optimized-v2", "reranker_url": "http://127.0.0.1:8999/rerank"}
    )

    class Ranker:
        def rank(self, query, texts):
            return [10.0 if "清理" in item else -2.0 for item in texts]

    hits = retrieval.search(
        session, actor, optimized, deps.embedder, query="会议室", kb_ids=[kb.id], reranker=Ranker()
    )
    assert hits[0].score_type == "rerank" and "清理" in hits[0].text
    with pytest.raises(ModelUnavailable):
        retrieval.search(session, actor, optimized, deps.embedder, query="会议室", kb_ids=[kb.id])


def test_retry_freezes_chunk_strategy_and_existing_build(session, settings, deps, actor):
    configured = settings.model_copy(update={"chunk_strategy": "structure-v2"})
    deps.settings = configured
    _, result = ingest(session, configured, deps, actor, "规则", "# 使用规则\n\n使用后归还。")
    original = session.get(IndexBuild, result.build_id)
    assert original.chunk_config["strategy"] == "structure-v2"
    document = document_service.detail(session, actor, result.document_id)
    rebuilt = document_service.reindex(
        session,
        actor,
        settings,
        document_id=result.document_id,
        version_id=result.version_id,
        expected_generation=document["generation"],
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    assert session.get(IndexBuild, rebuilt.build_id).chunk_config == original.chunk_config
    assert rebuilt.build_id != original.id


def test_evidence_budget_does_not_grow_after_primary_selection(session, settings, deps, actor):
    kb, _ = ingest(session, settings, deps, actor, "说明", "会议室使用后归还钥匙。")
    hit = retrieval.search(session, actor, settings, deps.embedder, query="会议室", kb_ids=[kb.id])[
        0
    ]
    hits = [replace(hit, chunk_id=str(uuid.uuid4()), text=f"自制内容{i}。") for i in range(10)]
    optimized = settings.model_copy(
        update={
            "retrieval_profile": "optimized-v2",
            "evidence_neighbor_limit": 0,
            "evidence_max_items": 3,
        }
    )
    assert len(retrieval.select_evidence(session, actor, optimized, [kb.id], hits)) == 3


def test_coverage_search_preserves_first_hit_and_checks_scope(session, settings, deps, actor):
    optimized = settings.model_copy(
        update={"chunk_strategy": "structure-v2", "retrieval_profile": "optimized-v2"}
    )
    deps.settings = optimized
    kb, _ = ingest(
        session, optimized, deps, actor, "签核安排", "HBR-271 的当班签核负责人是林松。"
    )
    second, _ = ingest(
        session, optimized, deps, actor, "交接安排", "HBR-271 的实体交接地点在西翼三号室。"
    )
    private, _ = ingest(
        session, optimized, deps, Actor("other", "default"), "私有记录",
        "HBR-271 的实体交接地点在南楼999室。",
    )
    query = "核对 HBR-271 的当班签核负责人、实体交接地点。"
    covered = optimized.model_copy(update={"evidence_coverage": True, "structured_context": True})
    scope = [kb.id, second.id]
    baseline = retrieval.search(
        session, actor, optimized, deps.embedder, query=query, kb_ids=scope
    )
    hits = retrieval.search(session, actor, covered, deps.embedder, query=query, kb_ids=scope)
    assert hits[0].chunk_id == baseline[0].chunk_id
    assert len({hit.chunk_id for hit in hits}) == len(hits)
    assert {hit.kb_id for hit in hits} == {str(kb.id), str(second.id)}
    assert {part for hit in hits for part in hit.diagnostics.get("evidence_requirements", [])} == {0, 1}
    with pytest.raises(NotFound):
        retrieval.search(
            session, actor, covered, deps.embedder, query=query, kb_ids=[kb.id, private.id]
        )
    single = "HBR-271 的实体交接地点在哪里？"
    assert retrieval.search(
        session, actor, covered, deps.embedder, query=single, kb_ids=scope
    ) == retrieval.search(session, actor, optimized, deps.embedder, query=single, kb_ids=scope)
