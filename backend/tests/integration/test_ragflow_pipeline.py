"""在隔离 PostgreSQL 验证新索引、首轮和补召回及原文引用。"""

import io
import math
import os
import uuid

import pytest
from docx import Document as WordDocument
from docx.oxml import OxmlElement
from docx.oxml.ns import qn
from fastapi.testclient import TestClient
from fpdf import FPDF
from openpyxl import Workbook
from sqlalchemy import delete, func, select, text, update
from sqlalchemy.orm import Session

from knowledge.adapters import ragflow_text
from knowledge.adapters.generator import StubGenerator
from knowledge.adapters.parser import build_parser_registry
from knowledge.adapters.ragflow_text import query_terms
from knowledge.api import deps as api_deps
from knowledge.domain.entities import Actor, Locator, ParsedBlock
from knowledge.domain.errors import KeywordIndexUnavailable, ModelUnavailable, NotFound
from knowledge.domain.ragflow_query import LEXICAL_REVISION
from knowledge.entrypoints.api import create_app
from knowledge.mcp import tools as mcp_tools
from knowledge.repositories import ragflow_search
from knowledge.repositories.tables import Chunk, ChunkSearchTerms, IndexBuild, Job
from knowledge.services import document, knowledge, qa, retrieval
from knowledge.worker.loop import process_next
from scripts.backfill_ragflow_terms import backfill, count_missing_terms
from scripts.local_runtime import LocalRuntime

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated database required"),
]


def test_native_candidates_are_reauthorized_against_current_publication(session, settings, deps, actor, monkeypatch):
    from scripts.real_model import check_ragflow_resources
    check_ragflow_resources()
    from knowledge.adapters.ragflow_opensearch import NativeIndex
    from knowledge.domain.errors import KeywordIndexUnavailable

    kb = knowledge.create(session, actor, "原生索引授权")
    allowed = _upload(session, actor, deps, kb, "allowed.txt", b"pressure valve current")
    foreign_actor = Actor("native-other-user", "default")
    foreign_kb = knowledge.create(session, foreign_actor, "其他用户")
    foreign = _upload(session, foreign_actor, deps, foreign_kb, "secret.txt", b"pressure valve foreign")
    own_chunk = session.scalar(select(Chunk).where(Chunk.build_id == allowed.build_id))
    other_chunk = session.scalar(select(Chunk).where(Chunk.build_id == foreign.build_id))
    assert own_chunk is not None and other_chunk is not None
    config = settings.model_copy(update={"search_engine": "opensearch"})
    seen = []
    monkeypatch.setattr(NativeIndex, "missing", lambda *_: [])

    def search_index(self, query, builds, *args, **kwargs):
        seen.extend(builds)
        # 模拟索引返回错误用户或已删除的陈旧候选；回源必须阻断。
        return [(other_chunk.id, 100.0), (own_chunk.id, 1.0)]

    monkeypatch.setattr(NativeIndex, "search", search_index)
    hits = retrieval.search(session, actor, config, deps.embedder, query="pressure valve", kb_ids=[kb.id], mode="keyword")
    assert [h.chunk_id for h in hits] == [str(own_chunk.id)]
    assert foreign.build_id not in seen and allowed.build_id in seen
    document.delete(session, actor, allowed.document_id, settings.max_attempts)
    assert not retrieval.search(session, actor, config, deps.embedder, query="pressure valve", kb_ids=[kb.id], mode="keyword")
    assert process_next(session, deps)  # 排空删除清理，后续上传 helper 才会领取新构建。
    with pytest.raises(NotFound):
        retrieval.search(session, actor, config, deps.embedder, query="pressure", kb_ids=[foreign_kb.id], mode="keyword")
    monkeypatch.setattr(NativeIndex, "missing", lambda _, builds: list(builds))
    still_active = _upload(session, actor, deps, kb, "new.txt", b"pressure valve")
    assert still_active
    with pytest.raises(KeywordIndexUnavailable):
        retrieval.search(session, actor, config, deps.embedder, query="pressure", kb_ids=[kb.id], mode="keyword")


def test_native_index_failure_does_not_publish_over_the_current_version(session, settings, deps, actor, monkeypatch):
    from scripts.real_model import check_ragflow_resources
    check_ragflow_resources()
    from knowledge.adapters.ragflow_opensearch import NativeIndex
    from knowledge.repositories.tables import Document

    kb = knowledge.create(session, actor, "原生发布失败")
    current = _upload(session, actor, deps, kb, "manual.txt", b"pressure original")
    deps.settings = settings.model_copy(update={"search_engine": "opensearch", "max_attempts": 1})
    pending = document.add_version(
        session, actor, deps.settings, deps.storage, deps.registry,
        document_id=current.document_id, filename="manual.txt", data=b"pressure replacement",
        expected_generation=None, embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    monkeypatch.setattr(NativeIndex, "missing", lambda _, builds: list(builds))

    def fail_index(*args, **kwargs):
        raise KeywordIndexUnavailable("synthetic index outage")

    monkeypatch.setattr(NativeIndex, "index", fail_index)
    assert process_next(session, deps)
    session.expire_all()
    stored = session.get(Document, current.document_id)
    assert stored.active_build_id == current.build_id and stored.current_version_id == current.version_id
    assert session.get(Job, pending.job_id).state.value == "failed"


def _upload(session, actor, deps, kb, filename, data):
    ack = document.upload(
        session, actor, deps.settings, deps.storage, deps.registry,
        kb_id=kb.id, filename=filename, data=data,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps)
    session.expire_all()
    return ack


def _historical_upload(session, actor, deps, kb, filename, data):
    """在隔离库模拟已有旧分块，不依赖已移除的旧构建算法。"""
    ack = _upload(session, actor, deps, kb, filename, data)
    build = session.get(IndexBuild, ack.build_id)
    build.chunk_config = {**build.chunk_config, "strategy": "legacy-v1"}
    session.execute(delete(ChunkSearchTerms).where(ChunkSearchTerms.chunk_id.in_(
        select(Chunk.id).where(Chunk.build_id == ack.build_id),
    )))
    session.commit()
    return ack


def test_publish_index_retry_query_and_authorization(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "自制查询数据")
    ack = _upload(session, actor, deps, kb, "routing.md", b"# Pump pressure\n\nRX-840 pressure valve is closed.")
    assert session.get(IndexBuild, ack.build_id).state.value == "ready"
    assert session.scalar(select(func.count()).select_from(ChunkSearchTerms)) == 1
    result = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-840 pressure valve",
        kb_ids=[kb.id], mode="hybrid",
    )
    assert result and result[0].score_type == "ragflow_hybrid"
    assert result[0].diagnostics["recall_retry"] is False
    assert result[0].diagnostics["lexical_rank"] == 1
    # 上游的长英文词项查询没有中文分支的匹配比例。
    broad_query = "RX-840 pressure unrelated aster nova ruby amber basil copper delta"
    broad_trace = {}
    broad = ragflow_search.recall(
        session, actor, kb_ids=[kb.id], document_ids=[], terms=query_terms(broad_query), mode="hybrid",
        vector=deps.embedder.embed([broad_query])[0], model=deps.embedder.model,
        dimension=deps.embedder.dimension, output_limit=5,
        gold=[uuid.UUID(result[0].chunk_id)], trace=broad_trace,
    )
    assert broad_trace["keyword_gate"] and broad_trace["shortlist"]
    assert broad_trace["recall_retry"] is False
    # 大部分查询词未命中，候选仍须通过最终相关度门槛。
    assert not broad
    query = "RX-840 pressure 苹果 橙子 洋葱 青椒 瓜子 香菜 海胆 鲨鱼"
    trace = {}
    ragflow_search.recall(
        session, actor, kb_ids=[kb.id], document_ids=[], terms=query_terms(query), mode="hybrid",
        vector=deps.embedder.embed([query])[0], model=deps.embedder.model,
        dimension=deps.embedder.dimension, output_limit=5,
        gold=[uuid.UUID(result[0].chunk_id)], trace=trace,
    )
    assert trace["recall_retry"] is True and trace["lexical_top"] is True
    assert not retrieval.search(
        session, actor, deps.settings, deps.embedder, query="saxophone",
        kb_ids=[kb.id], mode="hybrid",
    )
    answer = qa.answer(
        session, actor, deps.settings, deps.embedder, StubGenerator(),
        question="RX-840 pressure valve", kb_ids=[kb.id],
    )
    assert answer.citations[0].build_id == str(ack.build_id)
    assert "RX-840 pressure valve is closed." in answer.citations[0].excerpt
    with pytest.raises(NotFound):
        retrieval.search(
            session, Actor("other", "default"), deps.settings, deps.embedder,
            query="RX-840 pressure valve", kb_ids=[kb.id],
        )
    with pytest.raises(NotFound):
        retrieval.search(
            session, actor, deps.settings, deps.embedder, query="RX-840 pressure valve",
            kb_ids=[kb.id], document_ids=[uuid.uuid4()],
        )
    document.delete(session, actor, ack.document_id, 3)
    assert not retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-840",
        kb_ids=[kb.id], mode="keyword",
    )


def test_empty_semantic_fallback_is_reported_by_search_and_qa(session, settings, deps, actor, monkeypatch):
    configured = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "空知识库")
    query = "!!!"
    assert not query_terms(query).has_recall
    generator = StubGenerator()

    def unexpected_generation(*_):
        pytest.fail("没有证据时不能调用生成模型")

    monkeypatch.setattr(generator, "generate", unexpected_generation)
    app = create_app()

    def fixed(value):
        return lambda: value

    for dependency, value in [
        (api_deps.get_actor, actor),
        (api_deps.get_session, session),
        (api_deps.get_settings_dep, configured),
        (api_deps.get_embedder, deps.embedder),
        (api_deps.get_generator, generator),
    ]:
        app.dependency_overrides[dependency] = fixed(value)

    scope = {"knowledge_base_ids": [str(kb.id)]}
    with TestClient(app) as client:
        for mode in ("hybrid", "keyword", "semantic"):
            response = client.post("/api/v1/search", json={**scope, "query": query, "mode": mode})
            assert response.status_code == 200
            data = response.json()["data"]
            assert data["mode"] == mode and data["hits"] == []
            assert data["degraded"] == (mode == "hybrid")
            assert data["warnings"] == (["query_terms_empty"] if mode == "hybrid" else [])
        response = client.post("/api/v1/qa", json={**scope, "question": query})
        assert response.status_code == 200
        answer = response.json()["data"]
        assert answer["answer_status"] == "no_evidence" and answer["citations"] == []
        assert answer["retrieval"]["degraded"] and answer["retrieval"]["hits"] == 0
        assert answer["warnings"] == ["query_terms_empty"]

    mcp_result = mcp_tools.search(session, actor, configured, deps.embedder, query=query, kb_ids=[kb.id])
    assert mcp_result == {"warnings": ["query_terms_empty"], "degraded": True, "hits": []}


def test_local_runtime_serves_with_scoped_index_warning_and_recovers(session, deps, actor, monkeypatch):
    kb = knowledge.create(session, actor, "启动前索引检查")
    ack = _historical_upload(session, actor, deps, kb, "old.txt", b"Legacy pump manual.")
    row = session.scalar(select(Chunk).where(Chunk.build_id == ack.build_id))
    assert session.get(ChunkSearchTerms, row.id) is None

    url = deps.settings.database_url
    # 夹具已建立最新 schema；这里只验证迁移之后的真实词项检查和启动顺序。
    monkeypatch.setattr("scripts.local_runtime.subprocess.run", lambda *_args, **_kwargs: None)
    runtime = LocalRuntime(database_url=url, retrieval_profile="ragflow-v1")
    with runtime:
        assert {"api", "worker"} <= runtime.processes.keys()
        with runtime.client(actor.subject_id) as client, runtime.client("unrelated-owner") as other:
            assert client.get("/api/v1/search/status", headers={"Authorization": ""}).status_code == 401
            status = client.get("/api/v1/search/status")
            assert status.status_code == 200
            assert status.json()["data"] == {
                "status": "attention", "missing_keyword_chunks": 1, "legacy_pending_jobs": 0,
            }
            assert other.get("/api/v1/search/status").json()["data"] == {
                "status": "ready", "missing_keyword_chunks": 0, "legacy_pending_jobs": 0,
            }
            # 工作台资料管理仍可用；缺索引不得伪装成空检索。
            assert client.get(f"/api/v1/knowledge-bases/{kb.id}/documents").status_code == 200
            response = client.post("/api/v1/search", json={
                "query": "pump", "knowledge_base_ids": [str(kb.id)], "mode": "keyword",
            })
            assert response.status_code == 503
            assert response.json()["error"]["code"] == "KEYWORD_INDEX_UNAVAILABLE"
            assert session.get(ChunkSearchTerms, row.id) is None
            assert backfill(session) == 1
            assert client.get("/api/v1/search/status").json()["data"]["status"] == "ready"
            assert client.post("/api/v1/search", json={
                "query": "pump", "knowledge_base_ids": [str(kb.id)], "mode": "keyword",
            }).json()["data"]["hits"]
    pending = document.upload(
        session, actor, deps.settings, deps.storage, deps.registry, kb_id=kb.id,
        filename="pending.txt", data=b"Pending legacy task.", embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    build = session.get(IndexBuild, pending.build_id)
    build.chunk_config = {**build.chunk_config, "strategy": "legacy-v1"}
    session.commit()
    with LocalRuntime(database_url=url, retrieval_profile="ragflow-v1") as paused:
        assert "api" in paused.processes and "worker" not in paused.processes
        with paused.client(actor.subject_id) as client:
            assert client.get("/api/v1/search/status").json()["data"]["legacy_pending_jobs"] == 1

    def unexpected_read(*_):
        pytest.fail("旧策略任务不应再次读取或解析原件")

    monkeypatch.setattr(deps.storage, "get", unexpected_read)
    assert process_next(session, deps)
    session.expire_all()
    job = session.get(Job, pending.job_id)
    assert job.state.value == "failed" and job.last_error["code"] == "PARSE_FAILED"


def test_history_backfill_keeps_published_sources_and_vectors(session, settings, deps, actor):
    kb = knowledge.create(session, actor, "历史索引回填")
    ack = _historical_upload(session, actor, deps, kb, "policy.txt", b"Audit plum and DPT-771 stay in vault.")
    row = session.scalar(select(Chunk).where(Chunk.build_id == ack.build_id))
    assert row is not None
    original = (row.id, list(row.embedding), row.text, row.locator)
    configured = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    for mode in ("keyword", "hybrid"):
        with pytest.raises(KeywordIndexUnavailable, match="尚未回填或更新"):
            retrieval.search(session, actor, configured, deps.embedder, query="DPT-771", kb_ids=[kb.id], mode=mode)
    with pytest.raises(KeywordIndexUnavailable):
        qa.answer(session, actor, configured, deps.embedder, StubGenerator(), question="DPT-771", kb_ids=[kb.id])
    semantic = retrieval.search(
        session, actor, configured, deps.embedder, query=row.text, kb_ids=[kb.id], mode="semantic",
    )
    assert [hit.chunk_id for hit in semantic] == [str(row.id)]
    session.add(ChunkSearchTerms(
        chunk_id=row.id, terms=["obsolete"], boosted=[], fields={"content": ["obsolete"]},
    ))
    session.commit()
    with pytest.raises(KeywordIndexUnavailable):
        retrieval.search(session, actor, configured, deps.embedder, query="obsolete", kb_ids=[kb.id], mode="keyword")
    indexed = session.get(ChunkSearchTerms, row.id)
    assert indexed is not None
    indexed.fields = None
    indexed.lexical_revision = LEXICAL_REVISION
    session.commit()
    with pytest.raises(KeywordIndexUnavailable):
        retrieval.search(session, actor, configured, deps.embedder, query="DPT-771", kb_ids=[kb.id], mode="keyword")
    assert backfill(session, batch_size=1) == 1
    assert backfill(session, batch_size=1) == 0
    session.refresh(row)
    assert (row.id, list(row.embedding), row.text, row.locator) == original
    session.refresh(indexed)
    assert indexed.fields and "dpt-771" in indexed.fields["content"]
    assert indexed.fields["content_positions"]
    assert indexed.lexical_revision == LEXICAL_REVISION

    complete = indexed.fields.copy()
    assert "pair:and dpt-771" in complete["ranking"]
    assert "pair:plum dpt-771" not in complete["ranking"]
    indexed.fields = {**complete, "ranking": [*complete["ranking"], "pair:plum dpt-771"]}
    indexed.lexical_revision = LEXICAL_REVISION - 1
    session.commit()
    assert count_missing_terms(session) == 1
    assert backfill(session, batch_size=1) == 1
    session.refresh(indexed)
    assert indexed.fields == complete and indexed.lexical_revision == LEXICAL_REVISION

    for malformed in (
        {key: value for key, value in complete.items() if key != "ranking"},
        {**complete, "content_positions": {"legacy": True}},
    ):
        indexed.fields = malformed
        session.commit()
        assert count_missing_terms(session) == 1
        for mode in ("keyword", "hybrid"):
            with pytest.raises(KeywordIndexUnavailable, match="尚未回填或更新"):
                retrieval.search(session, actor, configured, deps.embedder, query="DPT-771", kb_ids=[kb.id], mode=mode)
        assert backfill(session, batch_size=1) == 1
        assert count_missing_terms(session) == 0
        session.refresh(indexed)
        assert indexed.fields == complete
    session.refresh(row)
    assert (row.id, list(row.embedding), row.text, row.locator) == original
    result = retrieval.search(
        session, actor, configured, deps.embedder, query="DPT-771",
        kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.chunk_id for hit in result] == [str(row.id)]
    assert not retrieval.search(
        session, actor, configured, deps.embedder, query="saxophone", kb_ids=[kb.id], mode="keyword",
    )


def test_ragflow_index_health_respects_current_document_scope(session, settings, deps, actor):
    kb = knowledge.create(session, actor, "混合历史发布")
    old = _historical_upload(session, actor, deps, kb, "old.txt", b"REF-773 service manual")
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    ready = _upload(session, actor, deps, kb, "current.txt", b"REF-773 inspected")

    with pytest.raises(KeywordIndexUnavailable):
        retrieval.search(session, actor, deps.settings, deps.embedder, query="REF-773", kb_ids=[kb.id], mode="keyword")
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="REF-773", kb_ids=[kb.id], mode="keyword",
        document_ids=[ready.document_id],
    )
    assert [hit.build_id for hit in hits] == [str(ready.build_id)]
    with pytest.raises(NotFound):
        retrieval.search(
            session, Actor("other", "default"), deps.settings, deps.embedder, query="REF-773", kb_ids=[kb.id],
            mode="keyword", document_ids=[old.document_id],
        )
    document.delete(session, actor, old.document_id, 3)
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="REF-773", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(ready.build_id)]


def test_vector_compatibility_checks_only_requested_current_builds(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    old_kb = knowledge.create(session, actor, "历史模型资料")
    current_kb = knowledge.create(session, actor, "当前模型资料")
    kb_ids = [old_kb.id, current_kb.id]
    old = _upload(session, actor, deps, old_kb, "old.txt", b"RX-901 pressure valve: old reading.")
    current = _upload(session, actor, deps, current_kb, "current.txt", b"RX-901 pressure valve: new reading.")
    # 写入流程拒绝混用模型，这里在隔离库模拟迁移前已经发布的异模型构建。
    session.get(IndexBuild, old.build_id).embedding_model = "legacy-fixture"
    session.commit()
    query = "RX-901 pressure valve"
    for mode in ("semantic", "hybrid"):
        for document_ids in ([], [old.document_id]):
            with pytest.raises(ModelUnavailable, match="索引与查询向量配置不兼容"):
                retrieval.search(
                    session, actor, deps.settings, deps.embedder, query=query, kb_ids=kb_ids,
                    mode=mode, document_ids=document_ids,
                )
        hits = retrieval.search(
            session, actor, deps.settings, deps.embedder, query=query, kb_ids=kb_ids,
            mode=mode, document_ids=[current.document_id],
        )
        assert [hit.build_id for hit in hits] == [str(current.build_id)]

    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query=query, kb_ids=kb_ids,
        mode="keyword", document_ids=[old.document_id],
    )
    assert [hit.build_id for hit in hits] == [str(old.build_id)]
    answer = qa.answer(
        session, actor, deps.settings, deps.embedder, StubGenerator(),
        question=query, kb_ids=kb_ids, document_ids=[current.document_id],
    )
    assert [citation.build_id for citation in answer.citations] == [str(current.build_id)]


def test_ragflow_checks_vector_compatibility_in_the_recall_snapshot(session, settings, deps, actor, monkeypatch):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "向量化期间发布变化")
    query = "RX-908 pressure valve"
    ack = _upload(session, actor, deps, kb, "manual.txt", query.encode())
    embed = deps.embedder.embed
    calls = []

    def embed_then_change_published_model(texts):
        calls.append(texts)
        result = embed(texts)
        # 独立事务模拟生成查询向量期间出现不同模型的已发布构建。
        with Session(session.get_bind()) as publisher:
            publisher.execute(update(IndexBuild).where(IndexBuild.id == ack.build_id).values(
                embedding_model="historical-fixture",
            ))
            publisher.commit()
        return result

    monkeypatch.setattr(deps.embedder, "embed", embed_then_change_published_model)
    for mode in ("semantic", "hybrid"):
        with pytest.raises(ModelUnavailable, match="索引与查询向量配置不兼容"):
            retrieval.search(
                session, actor, deps.settings, deps.embedder, query=query,
                kb_ids=[kb.id], document_ids=[ack.document_id], mode=mode,
            )
        assert calls == [[query]]
        calls.clear()
        session.execute(update(IndexBuild).where(IndexBuild.id == ack.build_id).values(
            embedding_model=deps.embedder.model,
        ))
        session.commit()


def test_scoped_semantic_search_ranks_selected_document_before_applying_limit(session, settings, deps, actor, monkeypatch):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "指定资料语义召回")
    data = "device,reading\n" + "".join(f"RX-910,{number}\n" for number in range(64))
    noise = _upload(session, actor, deps, kb, "others.csv", data.encode())
    target = _upload(session, actor, deps, kb, "selected.csv", b"device,reading\nRX-911,60\nRX-912,75\nRX-913,90\n")
    rows = session.scalars(select(Chunk).where(Chunk.build_id == target.build_id).order_by(Chunk.ordinal)).all()
    assert len(rows) == 4
    zeros = [0.0] * (deps.embedder.dimension - 2)
    query_vector = [1.0, 0.0, *zeros]
    session.execute(update(Chunk).where(Chunk.build_id == noise.build_id).values(embedding=query_vector))
    for row, vector in zip(rows, ([0.0, 1.0], [0.6, 0.8], [0.8, 0.6], [0.0, 0.0]), strict=True):
        row.embedding = [*vector, *zeros]
    session.commit()
    session.execute(text("SET LOCAL hnsw.ef_search = 1"))
    monkeypatch.setattr(deps.embedder, "embed", lambda texts: [query_vector])

    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-912", kb_ids=[kb.id],
        document_ids=[target.document_id], mode="semantic", top_k=2,
    )
    assert [hit.chunk_id for hit in hits] == [str(rows[2].id), str(rows[1].id)]
    assert [hit.score for hit in hits] == pytest.approx([0.8, 0.6])


def test_legacy_zero_embeddings_cannot_displace_a_valid_ragflow_candidate(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "异常历史向量")
    query = "VLV-460"
    data = "device,number\n" + "".join(f"{query},{number}\n" for number in range(65))
    history = _upload(session, actor, deps, kb, "history.csv", data.encode())
    rows = session.scalars(select(Chunk).where(Chunk.build_id == history.build_id)).all()
    assert len(rows) > 64
    for row in rows:
        row.embedding = [0.0] * deps.embedder.dimension
    session.commit()
    target = _upload(session, actor, deps, kb, "target.txt", query.encode())

    for mode in ("semantic", "hybrid"):
        hits = retrieval.search(
            session, actor, deps.settings, deps.embedder, query=query, kb_ids=[kb.id], mode=mode,
        )
        assert hits[0].build_id == str(target.build_id)
        assert all(math.isfinite(hit.score) and math.isfinite(hit.diagnostics["cosine"]) for hit in hits)
        if mode == "semantic":
            assert len(hits) == 1


def test_shortlist_maps_vector_score_but_uses_raw_cosine_for_threshold_and_final_score(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "预选分与最终分")
    query = "ZX-905"
    data = "device,reading\n" + "".join(f"{query},{number}\n" for number in range(64))
    noise = _upload(session, actor, deps, kb, "readings.csv", data.encode())
    target = _upload(session, actor, deps, kb, f"{query}.txt", query.encode())
    dimension = deps.embedder.dimension

    def embedding(cosine):
        return [cosine, math.sqrt(1 - cosine**2), *([0.0] * (dimension - 2))]

    noisy_rows = session.scalars(select(Chunk).where(Chunk.build_id == noise.build_id, Chunk.text.contains(query))).all()
    gold = session.scalar(select(Chunk).where(Chunk.build_id == target.build_id))
    assert len(noisy_rows) == 64 and gold is not None
    for row in noisy_rows:
        row.embedding = embedding(0.60)
    gold.embedding = embedding(0.57)
    session.commit()

    terms = query_terms(query)
    vector = embedding(1.0)
    trace = {}
    hits = ragflow_search.recall(
        session, actor, kb_ids=[kb.id], document_ids=[], terms=terms, mode="hybrid", vector=vector,
        model=deps.embedder.model, dimension=dimension, output_limit=64, gold=[gold.id], trace=trace,
    )
    assert trace["shortlist"] and trace["dense_top"]
    assert len(hits) == 64 and hits[0].chunk_id == str(gold.id)
    assert hits[0].diagnostics["cosine"] == pytest.approx(0.57)
    assert hits[0].diagnostics["preliminary"] == pytest.approx(0.05 * 0.5 + 0.95 * (1 + 0.57) / 2)
    indexed = session.get(ChunkSearchTerms, gold.id)
    assert hits[0].score == pytest.approx(0.7 * terms.coverage(indexed.fields["ranking"]) + 0.3 * 0.57)

    below = noisy_rows[0]
    below.embedding = embedding(0.19)
    session.commit()
    trace = {}
    ragflow_search.recall(
        session, actor, kb_ids=[kb.id], document_ids=[], terms=terms, mode="hybrid", vector=vector,
        model=deps.embedder.model, dimension=dimension, output_limit=64, gold=[below.id], trace=trace,
    )
    assert trace["lexical_top"] and not trace["dense_top"] and not trace["shortlist"]


def test_ragflow_caps_keyword_and_vector_lanes_independently(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "双路候选上限")
    query = "ZX-906"
    data = "device,reading\n" + "".join(f"{query},{number}\n" for number in range(1025))
    ack = _upload(session, actor, deps, kb, "readings.csv", data.encode())
    scoped = select(Chunk.id).where(Chunk.build_id == ack.build_id, Chunk.text.contains(query))
    ids = session.scalars(scoped.order_by(Chunk.ordinal)).all()
    assert len(ids) == 1025
    dimension = deps.embedder.dimension
    zeros = [0.0] * (dimension - 2)
    session.execute(update(Chunk).where(Chunk.id.in_(scoped)).values(embedding=[0.6, 0.8, *zeros]))
    session.execute(update(Chunk).where(Chunk.id == ids[0]).values(embedding=[0.28, 0.96, *zeros]))
    session.execute(update(Chunk).where(Chunk.id == ids[-1]).values(embedding=[0.8, 0.6, *zeros]))
    session.commit()

    for chunk_id, lexical, dense in ((ids[0], True, False), (ids[-1], False, True)):
        trace = {}
        hits = ragflow_search.recall(
            session, actor, kb_ids=[kb.id], document_ids=[], terms=query_terms(query),
            mode="hybrid", vector=[1.0, 0.0, *zeros], model=deps.embedder.model,
            dimension=dimension, output_limit=64, gold=[chunk_id], trace=trace,
        )
        assert trace["keyword_gate"]
        assert trace["lexical_top"] is lexical and trace["dense_top"] is dense
        assert len(hits) == 64 and hits[0].chunk_id == str(ids[-1])


def test_ragflow_rejects_zero_model_outputs_before_search_or_publication(session, settings, deps, actor, monkeypatch):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "无效模型返回")
    _upload(session, actor, deps, kb, "old.txt", b"RX-244 valve pressure")
    monkeypatch.setattr(deps.embedder, "embed", lambda texts: [[0.0] * deps.embedder.dimension for _ in texts])
    for mode in ("semantic", "hybrid"):
        with pytest.raises(ModelUnavailable, match="查询向量无效"):
            retrieval.search(
                session, actor, deps.settings, deps.embedder, query="RX-244", kb_ids=[kb.id], mode=mode,
            )
    assert retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-244", kb_ids=[kb.id], mode="keyword",
    )

    ack = _upload(session, actor, deps, kb, "pending.txt", b"RX-245 bearing")
    assert document.detail(session, actor, ack.document_id)["active_build_id"] is None
    assert session.scalar(select(func.count()).select_from(Chunk).where(Chunk.build_id == ack.build_id)) == 0


def test_markdown_code_fence_does_not_create_sections_or_lose_citation_ranges(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "代码围栏")
    code = "````text\r\n# 示例注释\r\n\r\n```\r\nRX-650 压力测试\r\n型号 | 示例\r\n--- | ---\r\n````\r\n"
    source = "# 设备手册\r\n\r\n" + code + "\r\n## 正式步骤\r\n复核设备记录。"
    ack = _upload(session, actor, deps, kb, "manual.md", source.encode())
    rows = session.scalars(select(Chunk).where(Chunk.build_id == ack.build_id)).all()
    code_row = next(row for row in rows if row.structure["kind"] == "code")
    assert code_row.text == code and code_row.structure["section_path"] == ["# 设备手册"]
    assert all(row.structure["kind"] != "table_row" for row in rows)
    assert all("# 示例注释" not in row.structure["section_path"] for row in rows)
    assert any(row.structure["section_path"] == ["# 设备手册", "## 正式步骤"] for row in rows)
    hit = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-650", kb_ids=[kb.id], mode="keyword",
    )[0]
    assert hit.chunk_id == str(code_row.id)
    locator = hit.locator["sources"][0]["locator"]
    assert source[locator["char_start"]:locator["char_end"]] == code


def test_markdown_table_indexes_real_columns_and_keeps_raw_row(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "Markdown 表格")
    row = r"| RX-651 | 温度\|145 | 通过 |"
    source = (
        "# 设备工况\r\n\r\n设备 | 工况 | 结果\r\n:--- | ---: | :---:\r\n"
        + row + "\r\n| RX-652 | 缺列 |\r\n\r\n"
        "设备 | 湿度\r\n--- | ---\r\nRX-653 | 60\r\n"
    )
    ack = _upload(session, actor, deps, kb, "records.md", source.encode())
    rows = session.scalars(select(Chunk).where(Chunk.build_id == ack.build_id)).all()
    by_text = {chunk.text: chunk for chunk in rows}
    selected = by_text[row]
    assert selected.structure["table_header"] == "设备\t工况\t结果"
    assert selected.structure["table_fields"] == ["设备: RX-651", r"工况: 温度\|145", "结果: 通过"]
    assert not by_text["| RX-652 | 缺列 |"].structure["table_fields"]
    assert by_text["RX-653 | 60"].structure["table_header"] == "设备\t湿度"
    indexed = session.get(ChunkSearchTerms, selected.id)
    assert "pair:温度 145" in indexed.terms
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-651", kb_ids=[kb.id], mode="keyword",
    )
    assert hits[0].chunk_id == str(selected.id)
    locator = hits[0].locator["sources"][0]["locator"]
    assert source[locator["char_start"]:locator["char_end"]] == row


def test_simple_pdf_uses_local_and_keeps_both_source_pages(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "简单 PDF")
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    for line in ("Certification sampling remains under", "controlled pressure during testing."):
        pdf.add_page()
        pdf.text(15, 20, line)
    ack = _upload(session, actor, deps, kb, "simple.pdf", bytes(pdf.output()))
    build = session.get(IndexBuild, ack.build_id)
    assert build.chunk_config["resolved_parser_profile"] == "legacy-v1"
    result = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="controlled pressure",
        kb_ids=[kb.id], mode="hybrid",
    )
    assert result[0].locator["pages"] == [1, 2]
    answer = qa.answer(
        session, actor, deps.settings, deps.embedder, StubGenerator(),
        question="controlled pressure", kb_ids=[kb.id],
    )
    assert answer.citations[0].locator["pages"] == [1, 2]
    for source, line in zip(result[0].locator["sources"], (
        "Certification sampling remains under", "controlled pressure during testing.",
    ), strict=True):
        assert result[0].text[source["chunk_start"]:source["chunk_end"]] == line


def test_mixed_pdf_freezes_mineru_on_retry_and_preserves_published_version(
    session, settings, deps, actor, monkeypatch,
):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "混排 PDF 重试")
    ack = _upload(session, actor, deps, kb, "manual.txt", b"TX-001 previous torque limit.")
    pdf = FPDF()
    pdf.set_font("Helvetica", size=12)
    pdf.add_page()
    pdf.text(15, 20, "This page contains a plain single column.")
    pdf.add_page()

    class MinerUFixture:
        calls = 0

        def supports(self, media_type, filename):
            return media_type == "application/pdf"

        def parse(self, data, media_type, filename):
            self.calls += 1
            if self.calls == 1:
                raise ModelUnavailable("测试中的短暂服务失败")
            body = "TX-002 revised torque limit."
            return [ParsedBlock(body, Locator(type="pdf", page=2, char_start=0, char_end=len(body)))]

    monkeypatch.setattr(deps.registry, "_mineru", MinerUFixture())
    pending = document.add_version(
        session, actor, deps.settings, deps.storage, deps.registry,
        document_id=ack.document_id, filename="mixed.pdf", data=bytes(pdf.output()),
        expected_generation=None, embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps)
    session.expire_all()
    job = session.get(Job, pending.job_id)
    assert job.state.value == "queued"
    assert session.get(IndexBuild, job.build_id).chunk_config["resolved_parser_profile"] == "mineru-api4.1-v1"
    assert document.detail(session, actor, ack.document_id)["active_build_id"] == str(ack.build_id)

    def unexpected_probe(*args):
        pytest.fail("选路已冻结，重试不应重新探测")

    monkeypatch.setattr(deps.registry, "probe_pdf", unexpected_probe)
    job.available_at = session.scalar(select(func.now()))
    session.commit()
    assert process_next(session, deps)
    session.expire_all()
    assert document.detail(session, actor, ack.document_id)["active_build_id"] == str(job.build_id)
    assert not retrieval.search(
        session, actor, deps.settings, deps.embedder, query="TX-001", kb_ids=[kb.id], mode="keyword",
    )
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="TX-002", kb_ids=[kb.id], mode="keyword",
    )
    assert hits[0].locator["page"] == 2


def test_table_column_and_value_are_indexed_together(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "表格词项配对")
    ack = _upload(session, actor, deps, kb, "table.csv", b"device,temperature\nRX-001,145\nRX-002,130\n")
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="temperature 145",
        kb_ids=[kb.id], mode="hybrid",
    )
    assert hits[0].build_id == str(ack.build_id)
    assert hits[0].locator["row_start"] == 2
    assert hits[0].text == "RX-001\t145"
    indexed = session.get(ChunkSearchTerms, uuid.UUID(hits[0].chunk_id))
    assert "pair:temperatur 145" in indexed.terms


def test_xlsx_merged_headers_keep_column_paths_and_real_rows(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1", "xlsx_rows_per_block": 1})
    deps.registry = build_parser_registry(deps.settings)
    book = Workbook()
    sheet = book.active
    sheet.title = "设备工况"
    sheet.append(["设备", "工况", None])
    sheet.append([None, "温度", "压力"])
    sheet.merge_cells("A1:A2")
    sheet.merge_cells("B1:C1")
    sheet.append(["RX-001", 145, 5])
    sheet.append([])
    sheet.append(["RX-002", "130\t摄氏度", 4])
    sheet.append(["RX-003", "共用读数", None])
    sheet.merge_cells("B6:C6")
    other = book.create_sheet("其他表")
    other.append(["设备", "湿度", "时段"])
    other.append(["RX-004", 58, "日班"])
    stream = io.BytesIO()
    book.save(stream)
    book.close()

    kb = knowledge.create(session, actor, "合并表头")
    ack = _upload(session, actor, deps, kb, "merged.xlsx", stream.getvalue())
    rows = session.scalars(select(Chunk).where(Chunk.build_id == ack.build_id)).all()
    by_source = {(row.locator["sheet"], row.locator["row_start"]): row for row in rows}
    assert set(by_source) == {("设备工况", row) for row in (1, 2, 3, 5, 6)} | {("其他表", 1), ("其他表", 2)}
    assert by_source["设备工况", 2].structure["table_header"] == ""
    selected = by_source["设备工况", 5]
    assert selected.text == "RX-002\t130\t摄氏度\t4"
    assert selected.structure["table_fields"] == ["设备: RX-002", "工况-温度: 130 摄氏度", "工况-压力: 4"]
    assert by_source["设备工况", 6].structure["table_fields"] == ["设备: RX-003"]
    assert by_source["其他表", 2].structure["table_header"] == "设备\t湿度\t时段"
    indexed = session.get(ChunkSearchTerms, selected.id)
    assert "pair:温度 130" in indexed.terms
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-002 温度 130", kb_ids=[kb.id], mode="hybrid",
    )
    assert hits[0].chunk_id == str(selected.id)
    assert hits[0].locator["sheet"] == "设备工况" and hits[0].locator["row_start"] == 5


def test_docx_table_uses_the_grid_to_pair_column_paths_and_preserve_source_rows(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    deps.registry = build_parser_registry(deps.settings)
    doc = WordDocument()
    doc.add_heading("工况记录", level=1)
    table = doc.add_table(rows=4, cols=4)
    table.cell(0, 0).merge(table.cell(1, 0)).text = "设备"
    table.cell(0, 1).merge(table.cell(0, 2)).text = "工况"
    table.cell(0, 3).merge(table.cell(1, 3)).text = "说明"
    table.cell(1, 1).text = "温度"
    table.cell(1, 2).text = "压力"
    for row in table.rows[:2]:
        row._tr.get_or_add_trPr().append(OxmlElement("w:tblHeader"))
    table.cell(2, 0).merge(table.cell(3, 0)).text = "RX-771"
    table.cell(2, 1).text = table.cell(2, 2).text = "145"
    table.cell(2, 3).text = "室内｜高温\t核验"
    table.cell(3, 1).merge(table.cell(3, 2)).text = "共用读数"
    table.cell(3, 3).text = "维护"
    other = doc.add_table(rows=2, cols=2)
    marker = OxmlElement("w:tblHeader")
    marker.set(qn("w:val"), "0")
    other.rows[0]._tr.get_or_add_trPr().append(marker)
    other.cell(0, 0).text, other.cell(0, 1).text = "设备", "湿度"
    other.cell(1, 0).text, other.cell(1, 1).text = "RX-772", "65"
    stream = io.BytesIO()
    doc.save(stream)

    kb = knowledge.create(session, actor, "DOCX 合并表头")
    ack = _upload(session, actor, deps, kb, "conditions.docx", stream.getvalue())
    rows = session.scalars(select(Chunk).where(Chunk.build_id == ack.build_id)).all()
    by_source = {
        (row.locator["block"], row.locator["row_start"]): row
        for row in rows if row.structure["kind"] == "table_row"
    }
    assert set(by_source) == {(1, 1), (1, 2), (1, 3), (1, 4), (2, 1), (2, 2)}
    selected = by_source[1, 3]
    assert selected.text == "表格1 第3行：RX-771｜145｜145｜室内｜高温\t核验"
    assert selected.locator["char_end"] == len(selected.text)
    assert selected.structure["section_path"] == ["工况记录"]
    assert selected.structure["table_fields"] == [
        "设备: RX-771", "工况-温度: 145", "工况-压力: 145", "说明: 室内｜高温 核验",
    ]
    assert by_source[1, 4].structure["table_fields"] == ["说明: 维护"]
    assert by_source[2, 2].structure["table_fields"] == []
    indexed = session.get(ChunkSearchTerms, selected.id)
    assert "pair:温度 145" in indexed.terms and "pair:压力 145" in indexed.terms
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="RX-771 温度 145", kb_ids=[kb.id], mode="keyword",
    )
    assert any(hit.chunk_id == str(selected.id) and hit.locator["row_start"] == 3 for hit in hits)


def test_keyword_search_uses_upstream_synonyms_and_field_weights(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "字段召回")
    alias = _upload(session, actor, deps, kb, "registry.txt", b"Identifier 873693 is listed.")
    direct = _upload(session, actor, deps, kb, "阿为特.txt", b"A pressure monitor is listed.")
    body = _upload(session, actor, deps, kb, "sensor.txt", "登记项为 阿为特。".encode())
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="阿为特", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(direct.build_id), str(body.build_id), str(alias.build_id)]
    assert hits[0].score > hits[1].score > hits[2].score > 0
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="873693", kb_ids=[kb.id], mode="keyword",
    )
    assert {hit.build_id for hit in hits} == {str(direct.build_id), str(body.build_id), str(alias.build_id)}


def test_wordnet_fallback_uses_local_corpus_in_keyword_recall(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "WordNet 词项召回")
    ack = _upload(session, actor, deps, kb, "transport.txt", b"The car was inspected.")
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="automobile", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(ack.build_id)]
    assert hits[0].text == "The car was inspected."


def test_whole_clause_alias_has_independent_weight_and_exact_positions(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "整句同义词")
    direct = _upload(session, actor, deps, kb, "direct.txt", b"USA")
    alias = _upload(session, actor, deps, kb, "short.txt", b"United States")
    full = _upload(session, actor, deps, kb, "long.txt", b"United States Army")
    _upload(session, actor, deps, kb, "gap.txt", b"United and States Army")
    _upload(session, actor, deps, kb, "reverse.txt", b"States United")
    _upload(session, actor, deps, kb, "United.txt", b"States Army")

    terms = query_terms("usa")
    assert terms.clause_aliases == {0: {
        "pair:unit state": pytest.approx(0.7 / 6.4),
        "phrase:unit state armi": pytest.approx(0.7 / 6.4),
    }}
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="usa", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(build.build_id) for build in (direct, full, alias)]
    assert [hit.score for hit in hits] == pytest.approx([0.1 * weight / 6.4 for weight in (5, 2.4, 1.7)])

    _upload(session, actor, deps, kb, "combined.txt", b"USA United States")
    another_clause = _upload(session, actor, deps, kb, "complete.txt", "United States 橙子".encode())
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder,
        query="usa 橙子 洋葱 青椒 瓜子 香菜 海胆", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(another_clause.build_id)]
    assert hits[0].diagnostics["coverage"] == pytest.approx(2 / 7)


def test_whole_clause_alias_can_recall_without_a_term_level_match(session, settings, deps, actor, monkeypatch):
    # 分开模拟词干与原始子句的词典结果，单独验证整句分支能进候选池。
    dictionary = {"galaxi": ("star cloud",), "galaxies": ("nebula of cluster",)}
    monkeypatch.setattr(ragflow_text, "synonyms_for", lambda word: dictionary.get(word, ()))
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "独立同义表达")
    exact = _upload(session, actor, deps, kb, "exact.txt", b"Nebula of cluster")
    _upload(session, actor, deps, kb, "other.txt", b"Nebula and cluster")

    terms = query_terms("galaxies")
    assert terms.groups == [["galaxi", "galaxies", "pair:star cloud"]]
    assert terms.clause_aliases == {0: {"phrase:nebula of cluster": pytest.approx(0.7 / 5.7)}}
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="galaxies", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(exact.build_id)]
    assert hits[0].score == pytest.approx(0.1 * 0.7 / 5.7)


def test_keyword_gate_counts_outer_clauses_and_retries_with_same_groups(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "子句召回")
    one_clause = _upload(session, actor, deps, kb, "single.txt", "长江大桥 维修".encode())
    two_clauses = _upload(session, actor, deps, kb, "double.txt", "长江 大桥 橙子".encode())
    query = "长江大桥维修 橙子 洋葱 青椒 瓜子 香菜 海胆 在 我 为"
    terms = query_terms(query)
    assert len(terms.clauses) == 7 and terms.clauses[0] == ("长江大桥", "维修")
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query=query, kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(two_clauses.build_id)]
    assert hits[0].diagnostics["coverage"] == pytest.approx(2 / 7)

    old_hit = session.scalar(select(Chunk.id).where(Chunk.build_id == one_clause.build_id))
    assert old_hit is not None
    trace = {}
    ragflow_search.recall(
        session, actor, kb_ids=[kb.id], document_ids=[one_clause.document_id], terms=terms, mode="hybrid",
        vector=deps.embedder.embed([query])[0], model=deps.embedder.model,
        dimension=deps.embedder.dimension, output_limit=5, gold=[old_hit], trace=trace,
    )
    assert trace["recall_retry"] and trace["keyword_gate"] and trace["lexical_top"]

    long_clause = "长江大桥维修橙子洋葱青椒瓜子香菜海胆"
    long_terms = query_terms(long_clause)
    assert len(long_terms.clauses) == 1 and len(long_terms.groups) >= 7
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query=long_clause, kb_ids=[kb.id], mode="keyword",
    )
    assert {hit.build_id for hit in hits} == {str(one_clause.build_id), str(two_clauses.build_id)}
    assert all(hit.diagnostics["coverage"] == 1.0 for hit in hits)


def test_whole_clause_phrase_scores_by_distance_and_requires_repeated_words(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "整句词位")
    variants = [
        ("exact.txt", "长江大桥 维修 橙子"),
        ("near.txt", "长江大桥 甲 维修 橙子"),
        ("slop.txt", "长江大桥 甲 维修 乙 橙子"),
        ("far.txt", "长江大桥 维修 甲 乙 丙 橙子"),
        ("repeat.txt", "长江大桥 维修 甲 长江大桥"),
    ]
    builds = {name: str(_upload(session, actor, deps, kb, name, body.encode()).build_id) for name, body in variants}
    query = "长江大桥维修橙子"
    assert query_terms(query).whole_phrases == {("长江大桥", "维修", "橙子"): pytest.approx(0.6)}
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query=query, kb_ids=[kb.id], mode="keyword",
    )
    by_build = {hit.build_id: hit.score for hit in hits}
    exact, near, slop, far = (by_build[builds[name]] for name, _ in variants[:4])
    assert exact > near > slop > far
    assert near - far == pytest.approx((exact - far) / 2)
    assert slop - far == pytest.approx((exact - far) / 3)

    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder,
        query="长江大桥维修长江大桥", kb_ids=[kb.id], mode="keyword",
    )
    by_build = {hit.build_id: hit.score for hit in hits}
    assert by_build[builds["repeat.txt"]] > by_build[builds["exact.txt"]]
    assert by_build[builds["exact.txt"]] == pytest.approx(by_build[builds["near.txt"]])


def test_recall_after_keyword_budget_uses_only_complete_phrases(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "长查询边界")
    exact = _upload(session, actor, deps, kb, "exact.txt", "橙子 洋葱".encode())
    near = _upload(session, actor, deps, kb, "near.txt", "橙子 甲 乙 洋葱".encode())
    _upload(session, actor, deps, kb, "far.txt", "橙子 甲 乙 丙 洋葱 火山".encode())
    query = "在 " * 32 + "火山 橙子洋葱"
    terms = query_terms(query)
    assert not terms.groups and terms.has_recall
    assert "火山" in terms.features and terms.clause_phrases == {0: ("橙子", "洋葱")}
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query=query, kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(exact.build_id), str(near.build_id)]
    assert all(hit.diagnostics["coverage"] == 1.0 for hit in hits)

    english = _upload(session, actor, deps, kb, "english.txt", b"Plum fig")
    english_gap = _upload(session, actor, deps, kb, "english-gap.txt", b"Plum and fig")
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder,
        query="alpha " * 256 + "plum fig", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(english.build_id)]
    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder,
        query="x" * 1100 + " plum fig stone", kb_ids=[kb.id], mode="keyword",
    )
    assert [hit.build_id for hit in hits] == [str(english.build_id), str(english_gap.build_id)]


def test_chinese_fine_tokens_require_an_exact_or_near_phrase(session, settings, deps, actor):
    deps.settings = settings.model_copy(update={"retrieval_profile": "ragflow-v1"})
    kb = knowledge.create(session, actor, "中文短语召回")
    direct = _upload(session, actor, deps, kb, "direct.txt", "长江大桥".encode())
    adjacent = _upload(session, actor, deps, kb, "adjacent.txt", "长江 大桥".encode())
    near = _upload(session, actor, deps, kb, "near.txt", "长江 壮观 大桥".encode())
    swapped = _upload(session, actor, deps, kb, "swapped.txt", "大桥 长江".encode())
    _upload(session, actor, deps, kb, "distant.txt", "长江 的 了 吗 大桥".encode())
    _upload(session, actor, deps, kb, "partial.txt", "长江".encode())
    _upload(session, actor, deps, kb, "长江.txt", "大桥".encode())

    hits = retrieval.search(
        session, actor, deps.settings, deps.embedder, query="长江大桥", kb_ids=[kb.id], mode="keyword",
    )
    assert {hit.build_id for hit in hits[:2]} == {str(direct.build_id), str(adjacent.build_id)}
    assert {hit.build_id for hit in hits[2:]} == {str(near.build_id), str(swapped.build_id)}
    assert hits[0].score == hits[1].score > hits[2].score == hits[3].score > 0

    terms = query_terms("长江大桥")
    assert terms.coverage(["长江", "大桥"]) > terms.coverage(["长江"]) > 0
    embeddings = deps.embedder.embed(["长江大桥", "长江 大桥"])
    ranked = ragflow_search.recall(
        session, actor, kb_ids=[kb.id], document_ids=[], terms=terms, mode="hybrid",
        vector=[left + right for left, right in zip(*embeddings, strict=True)],
        model=deps.embedder.model, dimension=deps.embedder.dimension, output_limit=10,
    )
    candidates = {hit.build_id: hit for hit in ranked}
    for build, keys in (
        (direct, ["长江大桥"]), (adjacent, ["长江", "大桥", "pair:长江 大桥"]),
    ):
        hit = candidates[str(build.build_id)]
        assert hit.score == pytest.approx(0.7 * terms.coverage(keys) + 0.3 * hit.diagnostics["cosine"])

    indexed = session.get(ChunkSearchTerms, uuid.UUID(next(
        hit.chunk_id for hit in hits if hit.build_id == str(adjacent.build_id)
    )))
    indexed.fields = {
        key: value for key, value in indexed.fields.items() if key != "ranking" and not key.endswith("_positions")
    }
    indexed.lexical_revision = LEXICAL_REVISION - 1
    session.commit()
    assert backfill(session) == 1
    assert backfill(session) == 0
    session.refresh(indexed)
    assert indexed.lexical_revision == LEXICAL_REVISION
    assert indexed.fields["content_positions"]
    assert "pair:长江 大桥" in indexed.fields["ranking"]
