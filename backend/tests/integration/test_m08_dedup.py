"""M08：同库重复上传同字节不产生新版本；同名不同字节产生新版本。

前半句见 test_m01_pipeline.py 的 test_duplicate_upload_is_deduplicated。
"""

from __future__ import annotations

import os

import pytest

from knowledge.domain.entities import Actor
from knowledge.repositories import documents as doc_repo
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.worker.loop import process_next
from samples import MEETING_RULES, VEHICLE_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_same_name_different_bytes_creates_new_version(session, settings, deps):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "制度库")

    first = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="名录.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps) is True

    second = document_service.add_version(
        session, actor, settings, deps.storage, deps.registry,
        document_id=first.document_id, filename="名录.md", data=VEHICLE_RULES,
        expected_generation=None,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )

    assert second.deduplicated is False
    assert second.version_id != first.version_id
    assert second.document_id == first.document_id

    versions = doc_repo.versions_of(session, first.document_id)
    assert [v.version_no for v in versions] == [1, 2]
    assert versions[0].sha256 != versions[1].sha256

    # generation 每次更新递增（发布栅栏依据）
    detail = document_service.detail(session, actor, first.document_id)
    assert detail["generation"] == 2
