"""孤儿对象回收：超过时限且无版本引用的对象在 Worker 启动扫描时删除。"""

from __future__ import annotations

import os
import time

import pytest

from knowledge.domain.entities import Actor
from knowledge.repositories import documents as doc_repo
from knowledge.services import cleanup
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from samples import MEETING_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_reclaim_removes_only_unreferenced_old_objects(session, settings, deps):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "制度库")
    upload = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="meeting-room.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )

    storage = deps.storage
    storage.put("orphan/old.md", b"stale")
    storage.put("orphan/fresh.md", b"recent")
    stale_time = time.time() - 2 * 3600
    os.utime(os.path.join(settings.storage_root, "orphan", "old.md"), (stale_time, stale_time))

    removed = cleanup.reclaim_orphans(session, storage, max_age_seconds=3600)

    assert removed == 1
    assert not storage.exists("orphan/old.md")
    # 未到时限的孤儿保留
    assert storage.exists("orphan/fresh.md")
    # 有版本引用的对象不回收
    versions = doc_repo.versions_of(session, upload.document_id)
    assert versions
    assert all(storage.exists(version.object_key) for version in versions)
