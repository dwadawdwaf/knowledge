"""M05：v2 处理中检索返回 v1 内容；发布后仅返回 v2。"""

from __future__ import annotations

import os

import pytest

from knowledge.domain.entities import Actor, BuildState
from knowledge.repositories.tables import IndexBuild
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import retrieval
from knowledge.worker.loop import process_next
from samples import MEETING_RULES, VEHICLE_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_v2_processing_keeps_v1_visible(session, settings, deps):
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
        query="借用会议室需要提前几天登记", kb_ids=[kb.id],
    )
    assert hits and "提前一天登记" in hits[0].text

    # 上传 v2（同名不同字节），未处理
    v2 = document_service.add_version(
        session, actor, settings, deps.storage, deps.registry,
        document_id=v1.document_id, filename="制度.md", data=VEHICLE_RULES,
        expected_generation=None,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    assert v2.deduplicated is False

    # v2 处理中：检索仍只看到 v1；v2 内容不可见
    during = retrieval.search(
        session, actor, settings, deps.embedder,
        query="公务用车如何调度", kb_ids=[kb.id],
    )
    assert during == []
    v1_still = retrieval.search(
        session, actor, settings, deps.embedder,
        query="借用会议室需要提前几天登记", kb_ids=[kb.id],
    )
    assert v1_still and "提前一天登记" in v1_still[0].text

    mid = document_service.detail(session, actor, v1.document_id)
    assert mid["active_build_id"] == str(v1.build_id)
    assert mid["pending_build_id"] == str(v2.build_id)
    assert mid["version_no"] == 1

    # 处理并发布 v2
    assert process_next(session, deps) is True

    after = retrieval.search(
        session, actor, settings, deps.embedder,
        query="公务用车如何调度", kb_ids=[kb.id],
    )
    assert after and "车队统一调度" in after[0].text

    # 旧内容不再命中：v1 构建已 retired
    old = retrieval.search(
        session, actor, settings, deps.embedder,
        query="借用会议室需要提前几天登记", kb_ids=[kb.id],
    )
    assert old == []

    published = document_service.detail(session, actor, v1.document_id)
    assert published["version_no"] == 2
    assert published["active_build_id"] == str(v2.build_id)
    assert published["pending_build_id"] is None
    assert session.get(IndexBuild, v1.build_id).state is BuildState.RETIRED
    assert session.get(IndexBuild, v2.build_id).state is BuildState.READY
