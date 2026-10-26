"""文档用例：上传、新版本、同模型重建、详情、删除。"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass

from sqlalchemy.orm import Session

from knowledge.adapters.storage import new_object_key
from knowledge.config import Settings
from knowledge.domain.entities import Actor, BuildState, JobKind, Lifecycle
from knowledge.domain.errors import (
    FileTooLarge,
    NotFound,
    UnsupportedFormat,
    VersionConflict,
    public_job_error,
)
from knowledge.domain.ports import ObjectStorage
from knowledge.repositories import documents as doc_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories import knowledge_bases as kb_repo
from knowledge.repositories import reindex as reindex_repo
from knowledge.repositories.tables import Document, IndexBuild


@dataclass(frozen=True)
class UploadResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    build_id: uuid.UUID
    job_id: uuid.UUID
    deduplicated: bool


@dataclass(frozen=True)
class ReindexResult:
    document_id: uuid.UUID
    version_id: uuid.UUID
    build_id: uuid.UUID
    job_id: uuid.UUID
    generation: int


def reindex(
    session: Session,
    actor: Actor,
    settings: Settings,
    *,
    document_id: uuid.UUID,
    version_id: uuid.UUID,
    expected_generation: int,
    embedder_model: str,
    embedder_dim: int,
) -> ReindexResult:
    """同模型重建最新上传版本；短事务内授权、比较代次并创建固定目标任务。"""
    document = reindex_repo.lock_document(session, actor, document_id)
    reindex_repo.get_version(session, actor, document_id, version_id)
    if (
        document.generation != expected_generation
        or reindex_repo.latest_version_id(session, actor, document_id) != version_id
        or reindex_repo.has_open_job(session, actor, document_id)
    ):
        raise VersionConflict("文档或处理状态已变化，请刷新后查询任务")
    source = reindex_repo.original_build(session, actor, version_id)
    if source is None or (source.embedding_model, source.embedding_dim) != (
        embedder_model,
        embedder_dim,
    ):
        raise VersionConflict("重建须使用原向量配置")
    build, job, generation = reindex_repo.enqueue(
        session,
        actor,
        document_id,
        source,
        settings.max_attempts,
    )
    result = ReindexResult(document_id, version_id, build.id, job.id, generation)
    session.commit()
    return result


def _new_build(
    session: Session,
    version_id: uuid.UUID,
    settings: Settings,
    embedder_model: str,
    embedder_dim: int,
) -> IndexBuild:
    build = IndexBuild(
        version_id=version_id,
        state=BuildState.QUEUED,
        chunk_config={
            "max_chars": settings.chunk_max_chars,
            "overlap_chars": settings.chunk_overlap_chars,
        },
        embedding_model=embedder_model,
        embedding_dim=embedder_dim,
        warnings=[],
    )
    session.add(build)
    session.flush()
    return build


def _validate(data: bytes, settings: Settings, registry, filename: str):
    from knowledge.adapters.parser import detect_media_type

    if len(data) == 0:
        raise UnsupportedFormat("文件为空")
    if len(data) > settings.max_upload_bytes:
        raise FileTooLarge("文件超过大小上限")

    media_type = detect_media_type(data, filename)
    registry.for_file(media_type, filename)  # 不支持时抛 UnsupportedFormat
    return media_type


def upload(
    session: Session,
    actor: Actor,
    settings: Settings,
    storage: ObjectStorage,
    registry,
    *,
    kb_id: uuid.UUID,
    filename: str,
    data: bytes,
    embedder_model: str,
    embedder_dim: int,
) -> UploadResult:
    kb = kb_repo.get_owned(session, actor, kb_id)
    if kb.status != Lifecycle.ACTIVE:
        raise NotFound("知识库不存在")

    media_type = _validate(data, settings, registry, filename)
    sha256 = hashlib.sha256(data).hexdigest()
    title = filename.rsplit("/", 1)[-1][:300]

    existing = doc_repo.find_version_by_sha(session, kb.id, sha256)
    if existing is not None:
        document = session.get(Document, existing.document_id)
        if document is not None and document.status == Lifecycle.ACTIVE:
            latest = job_repo.latest_for_document(session, document.id)
            return UploadResult(
                document_id=document.id,
                version_id=existing.id,
                build_id=document.active_build_id or document.pending_build_id or uuid.uuid4(),
                job_id=latest.id if latest else uuid.uuid4(),
                deduplicated=True,
            )

    object_key = new_object_key(media_type)
    storage.put(object_key, data)

    document = doc_repo.create(session, kb.id, title)
    version = doc_repo.add_version(
        session,
        document,
        object_key=object_key,
        sha256=sha256,
        media_type=media_type,
        byte_size=len(data),
    )
    build = _new_build(session, version.id, settings, embedder_model, embedder_dim)
    job = job_repo.create(
        session,
        JobKind.INGEST,
        document.id,
        settings.max_attempts,
        build_id=build.id,
        generation=document.generation + 1,
    )

    document.generation += 1
    document.pending_build_id = build.id
    session.commit()

    return UploadResult(
        document_id=document.id,
        version_id=version.id,
        build_id=build.id,
        job_id=job.id,
        deduplicated=False,
    )


def add_version(
    session: Session,
    actor: Actor,
    settings: Settings,
    storage: ObjectStorage,
    registry,
    *,
    document_id: uuid.UUID,
    filename: str,
    data: bytes,
    expected_generation: int | None,
    embedder_model: str,
    embedder_dim: int,
) -> UploadResult:
    document = doc_repo.lock_owned(session, actor, document_id)
    if expected_generation is not None and expected_generation != document.generation:
        raise VersionConflict("文档已被修改，请刷新后重试")

    media_type = _validate(data, settings, registry, filename)
    sha256 = hashlib.sha256(data).hexdigest()

    existing = doc_repo.find_version_by_sha(session, document.kb_id, sha256)
    if existing is not None and existing.document_id == document.id:
        latest = job_repo.latest_for_document(session, document.id)
        return UploadResult(
            document_id=document.id,
            version_id=existing.id,
            build_id=document.pending_build_id or document.active_build_id or uuid.uuid4(),
            job_id=latest.id if latest else uuid.uuid4(),
            deduplicated=True,
        )

    object_key = new_object_key(media_type)
    storage.put(object_key, data)

    version = doc_repo.add_version(
        session,
        document,
        object_key=object_key,
        sha256=sha256,
        media_type=media_type,
        byte_size=len(data),
    )
    build = _new_build(session, version.id, settings, embedder_model, embedder_dim)
    job = job_repo.create(
        session,
        JobKind.INGEST,
        document.id,
        settings.max_attempts,
        build_id=build.id,
        generation=document.generation + 1,
    )

    document.generation += 1
    document.pending_build_id = build.id
    session.commit()

    return UploadResult(
        document_id=document.id,
        version_id=version.id,
        build_id=build.id,
        job_id=job.id,
        deduplicated=False,
    )


def detail(session: Session, actor: Actor, document_id: uuid.UUID) -> dict:
    document = doc_repo.get_owned(session, actor, document_id)
    version = (
        doc_repo.get_version(session, document.current_version_id)
        if document.current_version_id
        else None
    )
    pending = (
        session.get(IndexBuild, document.pending_build_id) if document.pending_build_id else None
    )
    latest = job_repo.latest_for_document(session, document.id)

    return {
        "id": str(document.id),
        "kb_id": str(document.kb_id),
        "title": document.title,
        "status": document.status.value,
        "generation": document.generation,
        "current_version_id": str(document.current_version_id)
        if document.current_version_id
        else None,
        "active_build_id": str(document.active_build_id) if document.active_build_id else None,
        "pending_build_id": str(document.pending_build_id) if document.pending_build_id else None,
        "version_no": version.version_no if version else None,
        "pending_build_state": pending.state.value if pending else None,
        "job": _job_dict(latest) if latest else None,
    }


def list_in_kb(session: Session, actor: Actor, kb_id: uuid.UUID) -> list[dict]:
    kb_repo.get_owned(session, actor, kb_id)
    return [detail(session, actor, document.id) for document in doc_repo.list_by_kb(session, kb_id)]


def delete(
    session: Session, actor: Actor, document_id: uuid.UUID, max_attempts: int
) -> tuple[uuid.UUID, str]:
    document = doc_repo.lock_owned(session, actor, document_id)
    document.status = Lifecycle.DELETING
    document.generation += 1
    document.active_build_id = None
    document.pending_build_id = None
    job_repo.cancel_open_for_document(session, document.id)
    job = job_repo.create(session, JobKind.CLEANUP, document.id, max_attempts)
    session.commit()
    return job.id, document.status.value


def _job_dict(job) -> dict:
    return {
        "id": str(job.id),
        "kind": job.kind.value,
        "state": job.state.value,
        "stage": job.stage,
        "attempt": job.attempt,
        "max_attempts": job.max_attempts,
        "last_error": public_job_error(job.last_error),
        "created_at": job.created_at.isoformat() if job.created_at else None,
    }
