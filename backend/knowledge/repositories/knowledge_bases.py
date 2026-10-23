"""知识库数据访问。所有查询强制带 owner_id 条件。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, Lifecycle
from knowledge.domain.errors import NotFound
from knowledge.repositories.tables import Document, KnowledgeBase


def create(session: Session, actor: Actor, name: str) -> KnowledgeBase:
    kb = KnowledgeBase(owner_id=actor.subject_id, name=name, status=Lifecycle.ACTIVE)
    session.add(kb)
    session.flush()
    return kb


def list_owned(session: Session, actor: Actor) -> list[KnowledgeBase]:
    stmt = (
        select(KnowledgeBase)
        .where(
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status != Lifecycle.DELETED,
        )
        .order_by(KnowledgeBase.created_at.desc())
    )
    return list(session.scalars(stmt))


def get_owned(session: Session, actor: Actor, kb_id: uuid.UUID) -> KnowledgeBase:
    kb = session.get(KnowledgeBase, kb_id)
    if kb is None or kb.owner_id != actor.subject_id or kb.status == Lifecycle.DELETED:
        raise NotFound("知识库不存在")
    return kb


def get_active(session: Session, actor: Actor, kb_ids: list[uuid.UUID]) -> list[KnowledgeBase]:
    """批量校验范围：任一 ID 不可访问即整体拒绝，不静默缩小范围。"""
    stmt = select(KnowledgeBase).where(
        KnowledgeBase.id.in_(kb_ids), KnowledgeBase.owner_id == actor.subject_id
    )
    found = {kb.id: kb for kb in session.scalars(stmt)}
    for kb_id in kb_ids:
        kb = found.get(kb_id)
        if kb is None or kb.status != Lifecycle.ACTIVE:
            raise NotFound("知识库不存在")
    return list(found.values())


def mark_deleting(session: Session, kb: KnowledgeBase) -> None:
    kb.status = Lifecycle.DELETING


def mark_deleted(session: Session, kb_id: uuid.UUID) -> None:
    kb = session.get(KnowledgeBase, kb_id)
    if kb is not None and kb.status == Lifecycle.DELETING:
        kb.status = Lifecycle.DELETED


def has_unfinished_documents(session: Session, kb_id: uuid.UUID) -> bool:
    stmt = (
        select(Document.id)
        .where(Document.kb_id == kb_id, Document.status != Lifecycle.DELETED)
        .limit(1)
    )
    return session.scalar(stmt) is not None
