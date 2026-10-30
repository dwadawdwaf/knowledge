"""文档与版本的数据访问。所有按 ID 的读取都经知识库归属校验。"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, Lifecycle
from knowledge.domain.errors import NotFound
from knowledge.repositories.tables import Document, DocumentVersion, KnowledgeBase


def create(session: Session, kb_id: uuid.UUID, title: str) -> Document:
    document = Document(kb_id=kb_id, title=title, status=Lifecycle.ACTIVE, generation=0)
    session.add(document)
    session.flush()
    return document


def next_version_no(session: Session, document_id: uuid.UUID) -> int:
    current = session.scalar(
        select(func.max(DocumentVersion.version_no)).where(
            DocumentVersion.document_id == document_id
        )
    )
    return int(current or 0) + 1


def add_version(
    session: Session,
    document: Document,
    *,
    object_key: str,
    sha256: str,
    media_type: str,
    byte_size: int,
) -> DocumentVersion:
    version = DocumentVersion(
        kb_id=document.kb_id,
        document_id=document.id,
        version_no=next_version_no(session, document.id),
        object_key=object_key,
        sha256=sha256,
        media_type=media_type,
        byte_size=byte_size,
    )
    session.add(version)
    session.flush()
    return version


def get_owned(session: Session, actor: Actor, document_id: uuid.UUID) -> Document:
    stmt = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            Document.id == document_id,
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.status == Lifecycle.ACTIVE,
        )
    )
    document = session.scalar(stmt)
    if document is None:
        raise NotFound("文档不存在")
    return document


def lock_owned(session: Session, actor: Actor, document_id: uuid.UUID) -> Document:
    stmt = (
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            Document.id == document_id,
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.status == Lifecycle.ACTIVE,
        )
        .with_for_update(of=Document)
    )
    document = session.scalar(stmt)
    if document is None:
        raise NotFound("文档不存在")
    return document


def lock_by_id(session: Session, document_id: uuid.UUID) -> Document | None:
    stmt = select(Document).where(Document.id == document_id).with_for_update(of=Document)
    return session.scalar(stmt)


def list_by_kb(session: Session, kb_id: uuid.UUID) -> list[Document]:
    stmt = (
        select(Document)
        .where(Document.kb_id == kb_id, Document.status == Lifecycle.ACTIVE)
        .order_by(Document.created_at.desc())
    )
    return list(session.scalars(stmt))


def list_by_kb_and_status(session: Session, kb_id: uuid.UUID, status: Lifecycle) -> list[Document]:
    stmt = select(Document).where(Document.kb_id == kb_id, Document.status == status)
    return list(session.scalars(stmt))


def find_version_by_sha(session: Session, kb_id: uuid.UUID, sha256: str) -> DocumentVersion | None:
    stmt = select(DocumentVersion).where(
        DocumentVersion.kb_id == kb_id, DocumentVersion.sha256 == sha256
    )
    return session.scalar(stmt)


def get_version(session: Session, version_id: uuid.UUID) -> DocumentVersion:
    version = session.get(DocumentVersion, version_id)
    if version is None:
        raise NotFound("文档版本不存在")
    return version


def versions_of(session: Session, document_id: uuid.UUID) -> list[DocumentVersion]:
    stmt = (
        select(DocumentVersion)
        .where(DocumentVersion.document_id == document_id)
        .order_by(DocumentVersion.version_no)
    )
    return list(session.scalars(stmt))
