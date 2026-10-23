"""M06：解析中终止 Worker，重启后任务恢复，chunk 不重复。"""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import text

from knowledge.domain.chunking import chunk_blocks
from knowledge.domain.entities import Actor, JobState
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import EMBEDDING_DIM, Chunk
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import retrieval
from knowledge.worker.loop import process_next
from samples import MEETING_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_expired_lease_recovered_without_duplicate_chunks(session, settings, deps):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "制度库")
    upload = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="meeting-room.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
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

    # 分块数量与直接分段的结果一致，无重复 ordinal
    blocks = deps.registry.for_file("text/markdown", "meeting-room.md").parse(
        MEETING_RULES, "text/markdown", "meeting-room.md"
    )
    expected = len(chunk_blocks(blocks, settings.chunk_max_chars, settings.chunk_overlap_chars))
    assert chunk_repo.count_for_build(session, upload.build_id) == expected

    duplicated = session.execute(
        text(
            "SELECT ordinal FROM chunks WHERE build_id = :build"
            " GROUP BY ordinal HAVING count(*) > 1"
        ),
        {"build": upload.build_id},
    ).all()
    assert duplicated == []

    hits = retrieval.search(
        session, actor, settings, deps.embedder,
        query="使用后需要做什么", kb_ids=[kb.id],
    )
    assert hits and "清理现场" in hits[0].text
