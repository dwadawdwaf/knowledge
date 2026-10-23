"""M01：上传 → 处理 → 检索 → 问答 → 引用定位。

需要真实 PostgreSQL（含 pgvector、pg_trgm）。设置 KS_TEST_DATABASE_URL 后运行：
    KS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/ks_test pytest -m integration
"""

from __future__ import annotations

import os
import uuid

import pytest

from knowledge.adapters.generator import StubGenerator
from knowledge.domain.entities import Actor, JobState
from knowledge.domain.errors import NotFound
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import jobs as job_repo
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import qa as qa_service
from knowledge.services import retrieval
from knowledge.worker.loop import process_next
from samples import MEETING_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_upload_to_citation(session, settings, deps):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "制度库")

    upload = document_service.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="meeting-room.md",
        data=MEETING_RULES,
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    assert upload.deduplicated is False

    assert process_next(session, deps) is True

    job = job_repo.get_owned(session, actor, upload.job_id)
    assert job.state is JobState.SUCCEEDED

    detail = document_service.detail(session, actor, upload.document_id)
    assert detail["active_build_id"] == str(upload.build_id)
    assert detail["pending_build_id"] is None

    hits = retrieval.search(
        session,
        actor,
        settings,
        deps.embedder,
        query="使用后需要做什么",
        kb_ids=[kb.id],
        mode="hybrid",
        top_k=5,
    )
    assert hits, "应当检索到内容"
    assert "清理现场" in hits[0].text

    answer = qa_service.answer(
        session,
        actor,
        settings,
        deps.embedder,
        StubGenerator(),
        question="会议室使用后需要做什么",
        kb_ids=[kb.id],
    )
    assert answer.status == "answered"
    assert answer.citations
    citation = answer.citations[0]
    assert citation.document_id == str(upload.document_id)
    assert citation.build_id == str(upload.build_id)
    assert citation.locator


def test_duplicate_upload_is_deduplicated(session, settings, deps):
    actor = Actor(subject_id="user-b", tenant_id="default")
    kb = knowledge_service.create(session, actor, "去重库")

    first = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="a.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    second = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="a-copy.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    assert second.deduplicated is True
    assert second.document_id == first.document_id


def test_unrelated_question_returns_no_evidence(session, settings, deps):
    """M03：证据不足时不调用生成模型，也不返回编造答案。"""
    actor = Actor(subject_id="user-c", tenant_id="default")
    kb = knowledge_service.create(session, actor, "空库")
    document = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="b.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    process_next(session, deps)
    assert job_repo.get_owned(session, actor, document.job_id).state is JobState.SUCCEEDED

    hits = retrieval.search(
        session, actor, settings, deps.embedder,
        query="年假报销上限是多少", kb_ids=[kb.id],
    )
    assert hits == []

    answer = qa_service.answer(
        session, actor, settings, deps.embedder, StubGenerator(),
        question="年假报销上限是多少", kb_ids=[kb.id],
    )
    assert answer.status == "no_evidence"
    assert answer.citations == []


def test_cross_user_access_denied(session, settings, deps):
    """M04：他人 ID 一律 404，不泄露资源是否存在。"""
    owner = Actor(subject_id="user-d", tenant_id="default")
    kb = knowledge_service.create(session, owner, "隔离库")
    upload = document_service.upload(
        session, owner, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="c.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    process_next(session, deps)

    intruder = Actor(subject_id="user-e", tenant_id="default")

    with pytest.raises(NotFound):
        document_service.detail(session, intruder, upload.document_id)

    with pytest.raises(NotFound):
        job_repo.get_owned(session, intruder, upload.job_id)

    hits = retrieval.search(
        session, owner, settings, deps.embedder,
        query="使用后需要做什么", kb_ids=[kb.id],
    )
    assert hits

    with pytest.raises(NotFound):
        chunk_repo.get_owned(session, intruder, uuid.UUID(hits[0].chunk_id))
