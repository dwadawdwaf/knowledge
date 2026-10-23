"""M07：处理中删除文档，重试不使其复活；cleanup 完成后原件不可读。"""

from __future__ import annotations

import os
import uuid

import pytest

from knowledge.domain.entities import Actor, JobState
from knowledge.domain.errors import NotFound
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import documents as doc_repo
from knowledge.repositories import jobs as job_repo
from knowledge.services import document as document_service
from knowledge.services import ingest, retrieval
from knowledge.services import knowledge as knowledge_service
from knowledge.worker.loop import process_next
from samples import MEETING_RULES, VEHICLE_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_delete_during_processing_blocks_and_cleans(session, settings, deps):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "制度库")

    v1 = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="制度.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps) is True

    hits = retrieval.search(
        session, actor, settings, deps.embedder,
        query="会议室使用后需要做什么", kb_ids=[kb.id],
    )
    assert hits
    v1_chunk_id = hits[0].chunk_id
    object_keys = [v.object_key for v in doc_repo.versions_of(session, v1.document_id)]
    assert object_keys

    # 上传 v2 并模拟处理中：任务已被领取，租约在死掉的 Worker 手里
    v2 = document_service.add_version(
        session, actor, settings, deps.storage, deps.registry,
        document_id=v1.document_id, filename="制度.md", data=VEHICLE_RULES,
        expected_generation=None,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    dead_token = uuid.uuid4()
    claimed = job_repo.claim_next(session, dead_token, settings.lease_seconds)
    assert claimed is not None
    assert claimed["id"] == v2.job_id

    # 删除：逻辑阻断在同一事务内完成
    cleanup_job_id, status = document_service.delete(
        session, actor, v1.document_id, settings.max_attempts
    )
    assert status == "deleting"

    # 删除即阻断：检索与预览立即不可见（硬约束 C7）
    assert retrieval.search(
        session, actor, settings, deps.embedder,
        query="会议室使用后需要做什么", kb_ids=[kb.id],
    ) == []
    with pytest.raises(NotFound):
        chunk_repo.get_owned(session, actor, uuid.UUID(v1_chunk_id))

    # v2 的任务被取消，不会被重试复活
    assert job_repo.get_owned(session, actor, v2.job_id).state is JobState.CANCELLED

    # 队列中只剩 cleanup，执行物理清理
    assert process_next(session, deps) is True
    assert job_repo.get_owned(session, actor, cleanup_job_id).state is JobState.SUCCEEDED
    assert job_repo.get_owned(session, actor, v2.job_id).state is JobState.CANCELLED

    # 清理完成：记录与原件都不在了，删除后不可访问
    with pytest.raises(NotFound):
        document_service.detail(session, actor, v1.document_id)
    assert doc_repo.versions_of(session, v1.document_id) == []
    assert chunk_repo.count_for_build(session, v1.build_id) == 0
    assert all(not deps.storage.exists(key) for key in object_keys)

    # 迟到的发布尝试不会复活文档：发布栅栏（generation/租约）拒绝
    ingest.run(
        session,
        job_id=v2.job_id,
        lease_token=dead_token,
        document_id=v1.document_id,
        settings=settings,
        storage=deps.storage,
        registry=deps.registry,
        embedder=deps.embedder,
    )
    assert retrieval.search(
        session, actor, settings, deps.embedder,
        query="公务用车如何调度", kb_ids=[kb.id],
    ) == []
    with pytest.raises(NotFound):
        document_service.detail(session, actor, v1.document_id)
