"""M06：解析中终止 Worker，重启后任务恢复，chunk 不重复。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import select, text

from knowledge.adapters.ragflow_text import count_tokens
from knowledge.domain.entities import Actor, BuildState, JobState
from knowledge.domain.errors import ModelTimeout, ModelTransientUnavailable, ModelUnavailable
from knowledge.domain.ragflow_chunking import chunk_ragflow
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import EMBEDDING_DIM, Chunk, IndexBuild
from knowledge.services import document as document_service
from knowledge.services import ingest, retrieval
from knowledge.services import knowledge as knowledge_service
from knowledge.worker.loop import process_next
from samples import MEETING_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


@pytest.mark.parametrize("failure", [ModelTimeout, ModelTransientUnavailable])
def test_transient_embedding_retry_keeps_build_and_avoids_reparse(session, settings, deps, monkeypatch, failure):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "重试库")
    upload = document_service.upload(
        session, actor, settings, deps.storage, deps.registry, kb_id=kb.id,
        filename="fixture.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    original = deps.embedder.embed
    calls = []

    def flaky(texts):
        calls.append(len(texts))
        if len(calls) == 1:
            raise failure("synthetic")
        return original(texts)

    monkeypatch.setattr(deps.embedder, "embed", flaky)
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    assert process_next(session, deps)
    session.expire_all()
    job = job_repo.get_owned(session, actor, upload.job_id)
    assert job.state is JobState.SUCCEEDED
    assert job.attempt == 1 and job.build_id == upload.build_id
    assert len(calls) == 2 and chunk_repo.count_for_build(session, upload.build_id) > 0


def test_embedding_retry_checks_lease_before_second_request(session, settings, deps, monkeypatch):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "租约库")
    upload = document_service.upload(
        session, actor, settings, deps.storage, deps.registry, kb_id=kb.id,
        filename="fixture.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    calls = []
    original_stage = job_repo.set_stage

    def stage(session, job_id, token, name):
        if name == "embedding" and calls:
            return False
        return original_stage(session, job_id, token, name)

    def fail(texts):
        calls.append(len(texts))
        raise ModelTimeout("synthetic")

    monkeypatch.setattr(deps.embedder, "embed", fail)
    monkeypatch.setattr(job_repo, "set_stage", stage)
    monkeypatch.setattr(ingest.time, "sleep", lambda _: None)
    assert process_next(session, deps)
    session.expire_all()
    assert len(calls) == 1
    assert job_repo.get_owned(session, actor, upload.job_id).state is JobState.CANCELLED
    assert chunk_repo.count_for_build(session, upload.build_id) == 0


def test_deterministic_embedding_failure_does_not_retry_within_stage(session, settings, deps, monkeypatch):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "确定错误库")
    upload = document_service.upload(
        session, actor, settings, deps.storage, deps.registry, kb_id=kb.id,
        filename="fixture.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    calls = []

    def fail(texts):
        calls.append(len(texts))
        raise ModelUnavailable("synthetic")

    monkeypatch.setattr(deps.embedder, "embed", fail)
    assert process_next(session, deps)
    assert len(calls) == 1
    assert chunk_repo.count_for_build(session, upload.build_id) == 0


def test_expired_lease_recovered_without_duplicate_chunks(session, settings, deps, query_vector):
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

    # 模拟 Worker 崩溃：领取任务后进程消失，租约未释放
    dead_token = uuid.uuid4()
    claimed = job_repo.claim_next(session, dead_token, settings.lease_seconds)
    assert claimed is not None
    assert claimed["id"] == upload.job_id

    # 模拟崩溃前已写入本次构建的部分分块
    for ordinal in (0, 1):
        session.add(
            Chunk(
                build_id=upload.build_id,
                ordinal=ordinal,
                text="崩溃残留",
                locator={},
                embedding=[0.0] * EMBEDDING_DIM,
                tsv="崩溃残留",
            )
        )
    session.commit()

    # 租约过期后，重启的 Worker 通过启动巡检接管
    session.execute(
        text("UPDATE jobs SET lease_expires_at = now() - interval '1 second' WHERE id = :id"),
        {"id": upload.job_id},
    )
    session.commit()
    assert job_repo.recover_expired(session) == 1

    # 新 Worker 完成任务，attempt 不回退
    assert process_next(session, deps) is True
    job = job_repo.get_owned(session, actor, upload.job_id)
    assert job.state is JobState.SUCCEEDED
    assert job.attempt == 2
    assert job.build_id != upload.build_id
    assert (
        session.get(IndexBuild, upload.build_id, populate_existing=True).state == BuildState.FAILED
    )

    # 分块数量与直接分段的结果一致，无重复 ordinal
    blocks = deps.registry.for_file("text/markdown", "meeting-room.md", structured_markdown=True).parse(
        MEETING_RULES, "text/markdown", "meeting-room.md"
    )
    expected = len(chunk_ragflow(blocks, count_tokens))
    assert chunk_repo.count_for_build(session, job.build_id) == expected
    assert chunk_repo.count_for_build(session, upload.build_id) == 2

    duplicated = session.execute(
        text(
            "SELECT ordinal FROM chunks WHERE build_id = :build"
            " GROUP BY ordinal HAVING count(*) > 1"
        ),
        {"build": job.build_id},
    ).all()
    assert duplicated == []

    expected = session.scalar(select(Chunk).where(
        Chunk.build_id == job.build_id, Chunk.text.contains("清理现场"),
    ))
    query_vector("使用后需要做什么", expected.embedding)
    hits = retrieval.search(
        session,
        actor,
        settings,
        deps.embedder,
        query="使用后需要做什么",
        kb_ids=[kb.id],
    )
    assert hits and "清理现场" in hits[0].text
