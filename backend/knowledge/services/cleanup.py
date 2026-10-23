"""清理用例：删除原件、分块、构建与版本，最后标记文档已删除。"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from knowledge.domain.entities import JobState, Lifecycle
from knowledge.domain.ports import ObjectStorage
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import documents as doc_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import Document, DocumentVersion, IndexBuild
from knowledge.services import knowledge as knowledge_service


def reclaim_orphans(session: Session, storage: ObjectStorage, max_age_seconds: float) -> int:
    """回收超过 max_age 仍无版本引用的对象（上传后事务失败遗留），返回回收数。"""
    referenced = {row[0] for row in session.execute(select(DocumentVersion.object_key))}
    removed = 0
    for object_key, age in storage.iter_objects():
        if age >= max_age_seconds and object_key not in referenced:
            storage.delete(object_key)
            removed += 1
    return removed


def run(
    session: Session,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    document_id: uuid.UUID,
    storage: ObjectStorage,
) -> None:
    document = session.get(Document, document_id)
    if document is None:
        job_repo.finish(session, job_id, lease_token, JobState.SUCCEEDED, "cleaning")
        return

    versions = doc_repo.versions_of(session, document_id)
    for version in versions:
        storage.delete(version.object_key)

    chunk_repo.delete_for_document(session, document_id)
    session.execute(
        delete(IndexBuild).where(
            IndexBuild.version_id.in_(select(DocumentVersion.id).where(
                DocumentVersion.document_id == document_id
            ))
        )
    )
    session.execute(delete(DocumentVersion).where(DocumentVersion.document_id == document_id))

    session.execute(
        text(
            "UPDATE documents SET status = 'deleted', current_version_id = NULL,"
            " active_build_id = NULL, pending_build_id = NULL, updated_at = now()"
            " WHERE id = :id"
        ),
        {"id": document_id},
    )
    document.status = Lifecycle.DELETED

    knowledge_service.finalize_library(session, document_id)
    session.commit()
    job_repo.finish(session, job_id, lease_token, JobState.SUCCEEDED, "cleaning")
