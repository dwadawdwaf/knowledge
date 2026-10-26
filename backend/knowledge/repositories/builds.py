"""通过租约保护构建访问，统一按 document、job、build 顺序加锁。"""

from __future__ import annotations

import uuid

from sqlalchemy import func, select, text
from sqlalchemy.orm import Session

from knowledge.domain.entities import BuildState, JobState, Lifecycle
from knowledge.domain.errors import ModelUnavailable, StaleJob
from knowledge.repositories.tables import Chunk, Document, DocumentVersion, IndexBuild, Job


def lock_job(session: Session, job_id: uuid.UUID) -> tuple[Document | None, Job | None]:
    resource_id = session.scalar(select(Job.resource_id).where(Job.id == job_id))
    document = session.scalar(
        select(Document)
        .where(Document.id == resource_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    job = session.scalar(
        select(Job)
        .where(Job.id == job_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    return document, job


def target_matches(document: Document | None, job: Job) -> bool:
    return (
        document is not None
        and document.status == Lifecycle.ACTIVE
        and document.generation == job.generation
        and document.pending_build_id == job.build_id
        and job.build_id is not None
    )


def lock_execution(
    session: Session,
    job_id: uuid.UUID,
    token: uuid.UUID,
) -> tuple[Document, Job, IndexBuild, DocumentVersion]:
    document, job = lock_job(session, job_id)
    now = session.execute(select(func.clock_timestamp())).scalar_one()
    if (
        job is None
        or job.state != JobState.RUNNING
        or job.lease_token != token
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
        or not target_matches(document, job)
    ):
        raise StaleJob("任务执行资格已失效")
    build = session.scalar(
        select(IndexBuild)
        .where(IndexBuild.id == job.build_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if build is None or build.state not in (BuildState.QUEUED, BuildState.RUNNING):
        raise StaleJob("构建不可写入")
    version = session.get(DocumentVersion, build.version_id)
    if version is None or version.document_id != job.resource_id:
        raise StaleJob("构建目标不匹配")
    assert document is not None
    return document, job, build, version


def publish(
    session: Session,
    *,
    job_id: uuid.UUID,
    token: uuid.UUID,
    document_id: uuid.UUID,
    build_id: uuid.UUID,
    version_id: uuid.UUID,
    generation: int,
) -> None:
    document, job, build, version = lock_execution(session, job_id, token)
    if (
        document.id != document_id
        or build.id != build_id
        or version.id != version_id
        or generation != job.generation
    ):
        raise StaleJob("发布目标不匹配")
    if document.active_build_id is not None:
        old = session.get(IndexBuild, document.active_build_id)
        if old is not None and old.state == BuildState.READY:
            old.state = BuildState.RETIRED
    document.current_version_id = version.id
    document.active_build_id = build.id
    document.pending_build_id = None
    build.state = BuildState.READY
    job.state = JobState.SUCCEEDED
    job.stage = "publishing"
    job.lease_token = None
    job.lease_expires_at = None
    job.last_error = None
    session.flush()


def retry_build(session: Session, document: Document, job: Job) -> bool:
    old = session.get(IndexBuild, job.build_id)
    if old is None or old.state not in (BuildState.QUEUED, BuildState.RUNNING):
        return False
    old.state = BuildState.FAILED
    build = IndexBuild(
        version_id=old.version_id,
        state=BuildState.QUEUED,
        chunk_config=dict(old.chunk_config),
        embedding_model=old.embedding_model,
        embedding_dim=old.embedding_dim,
        warnings=[],
    )
    session.add(build)
    session.flush()
    document.pending_build_id = build.id
    job.build_id = build.id
    return True


def guard_vector_index(session: Session, job_id: uuid.UUID, token: uuid.UUID) -> None:
    _, _, build, _ = lock_execution(session, job_id, token)
    # 将配置校验与插入串行化，避免空索引首次写入时混入不同配置。
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('knowledge.vector_config'))"))
    incompatible = session.scalar(
        select(IndexBuild.id)
        .where(
            (
                (IndexBuild.embedding_model != build.embedding_model)
                | (IndexBuild.embedding_dim != build.embedding_dim)
            ),
            select(Chunk.id).where(Chunk.build_id == IndexBuild.id).exists(),
        )
        .limit(1)
    )
    if incompatible is not None:
        raise ModelUnavailable("向量索引配置不兼容，请恢复原配置或离线重建索引")
