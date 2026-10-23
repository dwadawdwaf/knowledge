"""M02：问答引用可经分块读取回溯到对应版本与原文位置。"""

from __future__ import annotations

import os
import uuid

import pytest

from knowledge.adapters.generator import StubGenerator
from knowledge.domain.entities import Actor
from knowledge.repositories import chunks as chunk_repo
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import qa as qa_service
from knowledge.worker.loop import process_next
from samples import MEETING_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_citation_resolves_to_chunk_and_version(session, settings, deps):
    actor = Actor(subject_id="user-a", tenant_id="default")
    kb = knowledge_service.create(session, actor, "制度库")
    upload = document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="meeting-room.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps) is True

    answer = qa_service.answer(
        session, actor, settings, deps.embedder, StubGenerator(),
        question="会议室使用后需要做什么", kb_ids=[kb.id],
    )
    assert answer.status == "answered"
    assert answer.citations

    detail = document_service.detail(session, actor, upload.document_id)
    for citation in answer.citations:
        chunk = chunk_repo.get_owned(session, actor, uuid.UUID(citation.chunk_id))

        # 引用四标识与分块一致：引用只能指向本轮实际送入模型的分块
        assert chunk["document_id"] == citation.document_id
        assert chunk["version_id"] == citation.version_id
        assert chunk["build_id"] == citation.build_id
        # 引用指向已发布版本，而不是未发布的构建
        assert chunk["version_id"] == detail["current_version_id"]

        # excerpt 由服务端从分块原文截取，不是模型改写
        assert citation.excerpt
        assert citation.excerpt in chunk["text"]
        assert citation.locator == chunk["locator"]

        # locator 语义：页码从 1 起，字符偏移左闭右开
        locator = citation.locator
        if locator.get("page") is not None:
            assert locator["page"] >= 1
        if locator.get("char_start") is not None and locator.get("char_end") is not None:
            assert 0 <= locator["char_start"] < locator["char_end"]
