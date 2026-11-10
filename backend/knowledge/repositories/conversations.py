"""会话与消息的数据访问。"""

from __future__ import annotations

import uuid

from sqlalchemy import case, select
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, Lifecycle
from knowledge.domain.errors import NotFound
from knowledge.repositories.tables import Conversation, Message


def get_owned(session: Session, actor: Actor, conversation_id: uuid.UUID) -> Conversation:
    conversation = session.get(Conversation, conversation_id)
    if conversation is None or conversation.owner_id != actor.subject_id:
        raise NotFound("会话不存在")
    return conversation


def list_owned(session: Session, actor: Actor) -> list[Conversation]:
    """归档会话不进入历史列表；消息保留，可直接改回 active 恢复。"""
    stmt = (
        select(Conversation)
        .where(
            Conversation.owner_id == actor.subject_id,
            Conversation.status == Lifecycle.ACTIVE.value,
        )
        .order_by(Conversation.created_at.desc())
    )
    return list(session.scalars(stmt))


def create(session: Session, actor: Actor, title: str = "") -> Conversation:
    conversation = Conversation(owner_id=actor.subject_id, title=title[:200])
    session.add(conversation)
    session.flush()
    return conversation


def delete_owned(session: Session, actor: Actor, conversation_id: uuid.UUID) -> None:
    """硬删除会话；messages 外键 ON DELETE CASCADE，消息一并消失。

    归档（mark_archived）用于隐藏但仍可追溯；这里是用户主动删除，不做保留。
    无权与不存在同样走 get_owned 的 404。
    """
    conversation = get_owned(session, actor, conversation_id)
    session.delete(conversation)
    session.flush()


def mark_archived(session: Session, conversation: Conversation) -> None:
    """归档：消息与引用保留，仅从历史列表移除。"""
    conversation.status = Lifecycle.ARCHIVED.value


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
    # 一轮问答的两条消息在同一事务写入，created_at 可能相同；显式让 user 排在 assistant 前。
    stmt = (
        select(Message)
        .where(Message.conversation_id == conversation_id)
        .order_by(Message.created_at, case((Message.role == "user", 0), else_=1))
    )
    return list(session.scalars(stmt))
