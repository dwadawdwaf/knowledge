"""会话持久化：问答写入消息，刷新后可读取，且严格按所有者隔离。"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import text

from knowledge.adapters.generator import StubGenerator
from knowledge.domain.entities import Actor
from knowledge.domain.errors import NotFound
from knowledge.services import conversations as conversation_service
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


def test_conversation_history_is_persisted_and_owner_scoped(session, settings, deps):
    actor = Actor(subject_id="conv-user", tenant_id="default")
    other = Actor(subject_id="conv-other", tenant_id="default")
    kb = knowledge_service.create(session, actor, "会话库")
    document_service.upload(
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
    assert process_next(session, deps) is True

    conversation = conversation_service.create(session, actor, "会议室问题")
    answer = qa_service.answer(
        session,
        actor,
        settings,
        deps.embedder,
        StubGenerator(),
        question="会议室使用后需要做什么",
        kb_ids=[kb.id],
        conversation_id=conversation.id,
    )
    assert answer.status == "answered"

    items = conversation_service.list_owned(session, actor)
    assert [str(item.id) for item in items] == [str(conversation.id)]

    stored, messages = conversation_service.detail(session, actor, conversation.id)
    assert stored.title == "会议室问题"
    assert [message.role for message in messages] == ["user", "assistant"]
    assert messages[0].body == "会议室使用后需要做什么"
    assert messages[1].body == answer.text
    assert messages[1].answer_status == "answered"
    assert messages[1].citations
    assert messages[1].citations[0]["chunk_id"] == answer.citations[0].chunk_id

    assert conversation_service.list_owned(session, other) == []
    with pytest.raises(NotFound):
        conversation_service.detail(session, other, conversation.id)


def test_delete_removes_conversation_and_messages(session, settings, deps):
    """删除会话要连消息一起消失；他人删不动，删完再读 404。"""
    actor = Actor(subject_id="del-user", tenant_id="default")
    other = Actor(subject_id="del-other", tenant_id="default")
    kb = knowledge_service.create(session, actor, "删除会话库")
    document_service.upload(
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
    assert process_next(session, deps) is True

    conversation = conversation_service.create(session, actor, "待删除会话")
    qa_service.answer(
        session,
        actor,
        settings,
        deps.embedder,
        StubGenerator(),
        question="会议室使用后需要做什么",
        kb_ids=[kb.id],
        conversation_id=conversation.id,
    )
    _, messages = conversation_service.detail(session, actor, conversation.id)
    assert messages

    # 他人删除：视为不存在
    with pytest.raises(NotFound):
        conversation_service.delete(session, other, conversation.id)

    conversation_service.delete(session, actor, conversation.id)
    with pytest.raises(NotFound):
        conversation_service.detail(session, actor, conversation.id)
    assert conversation_service.list_owned(session, actor) == []
    leftover = session.execute(
        text("SELECT count(*) FROM messages WHERE conversation_id = :id"),
        {"id": conversation.id},
    ).scalar_one()
    assert leftover == 0
