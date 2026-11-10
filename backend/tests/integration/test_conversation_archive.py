"""归档会话：不进历史列表，但消息仍可读取，改回 active 即恢复。"""

from __future__ import annotations

import os

import pytest

from knowledge.domain.entities import Actor, Lifecycle
from knowledge.repositories import conversations as conversation_repo
from knowledge.repositories import knowledge_bases as kb_repo
from knowledge.services import conversations as conversation_service
from knowledge.services import knowledge as knowledge_service

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def test_archived_conversation_is_hidden_from_history(session):
    actor = Actor(subject_id="archive-conv", tenant_id="default")
    keep = conversation_repo.create(session, actor, "保留会话")
    archived = conversation_repo.create(session, actor, "第二轮 · 真实长文档")
    session.flush()

    conversation_repo.mark_archived(session, archived)
    session.flush()

    titles = [c.title for c in conversation_repo.list_owned(session, actor)]
    assert titles == ["保留会话"]

    # 详情仍可读，便于恢复与追溯
    assert conversation_repo.get_owned(session, actor, archived.id).status == Lifecycle.ARCHIVED.value

    archived.status = Lifecycle.ACTIVE.value
    session.flush()
    assert {c.title for c in conversation_repo.list_owned(session, actor)} == {"保留会话", "第二轮 · 真实长文档"}
    assert keep.status == Lifecycle.ACTIVE.value


def test_archived_kb_documents_stay_hidden_from_list(session):
    """归档知识库与归档会话互不影响：库归档后仍不在列表，但文档记录保留。"""
    actor = Actor(subject_id="archive-both", tenant_id="default")
    kb = knowledge_service.create(session, actor, "第二轮库")
    kb_repo.mark_archived(session, kb)
    session.flush()

    assert kb_repo.list_owned(session, actor) == []
    assert conversation_service.list_owned(session, actor) == []
