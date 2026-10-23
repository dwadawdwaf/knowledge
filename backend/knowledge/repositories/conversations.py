"""会话与消息的数据访问。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor
from knowledge.domain.errors import NotFound
from knowledge.repositories.tables import Conversation, Message


def get_owned(session: Session, actor: Actor, conversation_id: uuid.UUID) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.owner_id != actor.subject_id:
        raise NotFound("会话不存在")
    return conversation


def create(session: Session, actor: Actor, title: str = "") -> Conversation:
    conversation = Conversation(owner_id=actor.subject_id, title=title[:200])
    session.add(conversation)
    session.flush()
    return conversation


def append(
    session: Session,
    conversation_id: uuid.UUID,
    *,
    question: str,
    answer: str,
    answer_status: str,
    citations: list[dict],
    model_revision: str,
) -> None:
    session.add(
        Message(conversation_id=conversation_id, role="user", body=question)
    )
    session.add(
        Message(
            conversation_id=conversation_id,
            role="assistant",
            body=answer,
            answer_status=answer_status,
            citations=citations,
            model_revision=model_revision,
        )
    )
    session.flush()


def messages_of(session: Session, conversation_id: uuid.UUID) -> list[Message]:
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at)
    )
    return list(session.scalars(stmt))
