"""知识库用例。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, JobKind, Lifecycle
from knowledge.domain.errors import ValidationFailed
from knowledge.repositories import documents as doc_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories import knowledge_bases as kb_repo
from knowledge.repositories.tables import Document, KnowledgeBase


def create(session: Session, actor: Actor, name: str) -> KnowledgeBase:
    if not name.strip():
        raise ValidationFailed("知识库名称不能为空")
    kb = kb_repo.create(session, actor, name.strip()[:200])
    session.commit()
    return kb


def list_owned(session: Session, actor: Actor) -> list[KnowledgeBase]:
    return kb_repo.list_owned(session, actor)


def delete(session: Session, actor: Actor, kb_id: uuid.UUID, max_attempts: int) -> int:
    """阻断整库：标记删除、取消未完成任务、逐文档创建清理任务。返回清理任务数。"""
    kb = kb_repo.get_owned(session, actor, kb_id)
    kb_repo.mark_deleting(session, kb)

    created = 0
    for document in doc_repo.list_by_kb(session, kb.id):
        document.status = Lifecycle.DELETING
        document.generation += 1
        document.active_build_id = None
        document.pending_build_id = None
        job_repo.cancel_open_for_document(session, document.id)
        job_repo.create(session, JobKind.CLEANUP, document.id, max_attempts)
        created += 1

    session.commit()
    return created


def finalize_library(session: Session, document_id: uuid.UUID) -> None:
    """文档清理完成后，若知识库已无未完成文档则收尾。"""
    document = session.get(Document, document_id)
    if document is None:
        return
    if not kb_repo.has_unfinished_documents(session, document.kb_id):
        kb_repo.mark_deleted(session, document.kb_id)
    session.flush()
