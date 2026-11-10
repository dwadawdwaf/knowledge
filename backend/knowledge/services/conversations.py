"""会话与消息用例：创建、列出、读取历史；鉴权由 repository 统一带 owner 条件。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor
from knowledge.repositories import conversations as conversation_repo
from knowledge.repositories.tables import Conversation, Message


def create(session: Session, actor: Actor, title: str = "") -> Conversation:
    conversation = conversation_repo.create(session, actor, title)
    session.commit()
    return conversation


def list_owned(session: Session, actor: Actor) -> list[Conversation]:
    return conversation_repo.list_owned(session, actor)


def delete(session: Session, actor: Actor, conversation_id: uuid.UUID) -> None:
    conversation_repo.delete_owned(session, actor, conversation_id)
    session.commit()


def detail(
    session: Session, actor: Actor, conversation_id: uuid.UUID
) -> tuple[Conversation, list[Message]]:
    conversation = conversation_repo.get_owned(session, actor, conversation_id)
    return conversation, conversation_repo.messages_of(session, conversation_id)
