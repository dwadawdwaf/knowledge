"""重建数据访问：所有查询显式限定所有者与 active 资源。"""

from __future__ import annotations

import uuid

from sqlalchemy import select
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, BuildState, JobKind, JobState, Lifecycle
from knowledge.domain.errors import NotFound
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import Document, DocumentVersion, IndexBuild, Job, KnowledgeBase


def lock_document(session: Session, actor: Actor, document_id: uuid.UUID) -> Document:
    document = session.scalar(
        select(Document)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            Document.id == document_id,
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.status == Lifecycle.ACTIVE,
        )
        .with_for_update(of=Document)
        .execution_options(populate_existing=True)
    )
    if document is None:
        raise NotFound("文档不存在")
    return document


def get_version(
    session: Session,
    actor: Actor,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
) -> DocumentVersion:
    version = session.scalar(
        select(DocumentVersion)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            DocumentVersion.id == version_id,
            Document.id == document_id,
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.status == Lifecycle.ACTIVE,
        )
    )
    if version is None:
        raise NotFound("文档版本不存在")
    return version


def latest_version_id(session: Session, actor: Actor, document_id: uuid.UUID) -> uuid.UUID | None:
    return session.scalar(
        select(DocumentVersion.id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            Document.id == document_id,
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.status == Lifecycle.ACTIVE,
        )
        .order_by(DocumentVersion.version_no.desc())
        .limit(1)
    )


def original_build(session: Session, actor: Actor, version_id: uuid.UUID) -> IndexBuild | None:
    return session.scalar(
        select(IndexBuild)
        .join(DocumentVersion, DocumentVersion.id == IndexBuild.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            DocumentVersion.id == version_id,
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.status == Lifecycle.ACTIVE,
        )
        .order_by(IndexBuild.created_at, IndexBuild.id)
        .limit(1)
    )


def has_open_job(session: Session, actor: Actor, document_id: uuid.UUID) -> bool:
    return (
        session.scalar(
            select(Job.id)
            .join(Document, Document.id == Job.resource_id)
            .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
            .where(
                Document.id == document_id,
                KnowledgeBase.owner_id == actor.subject_id,
                KnowledgeBase.status == Lifecycle.ACTIVE,
                Document.status == Lifecycle.ACTIVE,
                Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
            )
            .limit(1)
        )
        is not None
    )


def enqueue(
    session: Session,
    actor: Actor,
    document_id: uuid.UUID,
    source: IndexBuild,
    max_attempts: int,
) -> tuple[IndexBuild, Job, int]:
    document = lock_document(session, actor, document_id)
    get_version(session, actor, document_id, source.version_id)
    build = IndexBuild(
        version_id=source.version_id,
        state=BuildState.QUEUED,
        chunk_config=dict(source.chunk_config),
        embedding_model=source.embedding_model,
        embedding_dim=source.embedding_dim,
        warnings=[],
    )
    session.add(build)
    session.flush()
    generation = document.generation + 1
    job = job_repo.create(
        session,
        JobKind.REINDEX,
        document.id,
        max_attempts,
        build_id=build.id,
        generation=generation,
    )
    document.generation = generation
    document.pending_build_id = build.id
    session.flush()
    return build, job, generation
