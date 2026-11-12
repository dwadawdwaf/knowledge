"""文档用例：上传、新版本、同模型重建、详情、删除。"""

from __future__ import annotations

import hashlib
import uuid
from dataclasses import dataclass
from typing import get_args

from sqlalchemy.orm import Session

from knowledge.adapters.preview import docx_preview, sheet_preview, to_png
from knowledge.adapters.storage import new_object_key
from knowledge.config import Settings
from knowledge.domain.entities import Actor, BuildState, JobKind, Lifecycle, ParserProfile
from knowledge.domain.errors import (
    FileTooLarge,
    NotFound,
    UnsupportedFormat,
    ValidationFailed,
    VersionConflict,
    public_job_error,
)
from knowledge.domain.ports import ObjectStorage
from knowledge.domain.ragflow_chunking import STRATEGY, TITLE_DEPTH, TOKEN_CAP
from knowledge.repositories import chunks as chunk_repo
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
    parser_profile: str = "legacy-v1"


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
    if source.chunk_config.get("strategy") != STRATEGY:
        raise VersionConflict("请上传新文件版本以使用新的分块流程")
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


def rebuild_current_version(
    session: Session,
    actor: Actor,
    *,
    document_id: uuid.UUID,
    embedder_model: str,
    embedder_dim: int,
    max_attempts: int,
    parser_profile: str | None = None,
) -> ReindexResult:
    """按现行分块策略与给定向量配置重建当前版本，供更换向量模型后的维护重使用。

    旧构建与片段不原地改动：新构建发布成功后旧构建才退役（C4）。默认解析方案沿用原构建，
    避免同一份原件被换成另一条解析路径；扫描件等例外可由调用方显式覆盖。
    """
    document = reindex_repo.lock_document(session, actor, document_id)
    if reindex_repo.has_open_job(session, actor, document_id):
        raise VersionConflict("文档仍有未完成任务")
    version_id = document.current_version_id
    if version_id is None:
        raise NotFound("文档没有可重建的版本")
    reindex_repo.get_version(session, actor, document_id, version_id)
    build = _new_build(
        session,
        version_id,
        embedder_model,
        embedder_dim,
        parser_profile or _existing_profile(session, document, pending_first=True),
    )
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
    session.commit()
    return ReindexResult(document_id, version_id, build.id, job.id, generation)


def _new_build(
    session: Session,
    version_id: uuid.UUID,
    embedder_model: str,
    embedder_dim: int,
    parser_profile: str,
) -> IndexBuild:
    build = IndexBuild(
        version_id=version_id,
        state=BuildState.QUEUED,
        chunk_config={
            "strategy": STRATEGY,
            "token_cap": TOKEN_CAP,
            "title_depth": TITLE_DEPTH,
            "overlap_tokens": 0,
            "parser_profile": parser_profile,
        },
        embedding_model=embedder_model,
        embedding_dim=embedder_dim,
        warnings=[],
    )
    session.add(build)
    session.flush()
    return build


def _validate(data: bytes, settings: Settings, registry, filename: str, profile: str | None = None):
    from knowledge.adapters.parser import detect_media_type

    if len(data) == 0:
        raise UnsupportedFormat("文件为空")
    if len(data) > settings.max_upload_bytes:
        raise FileTooLarge("文件超过大小上限")

    media_type = detect_media_type(data, filename)
    if profile is None:
        if media_type == "application/pdf":
            registry.for_file(media_type, filename, profile="legacy-v1")
            return media_type, "pdf-auto-v1"
        profile = "mineru-api4.1-v1" if media_type.startswith("image/") else "legacy-v1"
    selected = profile
    if selected not in get_args(ParserProfile):
        raise ValidationFailed("未知解析方案")
    registry.for_file(media_type, filename, profile=selected)
    return media_type, selected


def _existing_profile(session: Session, document: Document, *, pending_first: bool = False) -> str:
    build_id = (
        (document.pending_build_id or document.active_build_id)
        if pending_first
        else (document.active_build_id or document.pending_build_id)
    )
    build = session.get(IndexBuild, build_id) if build_id else None
    config = (build.chunk_config or {}) if build else {}
    return config.get("resolved_parser_profile", config.get("parser_profile", "legacy-v1"))


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
    parser_profile: str | None = None,
) -> UploadResult:
    kb = kb_repo.get_owned(session, actor, kb_id)
    if kb.status != Lifecycle.ACTIVE:
        raise NotFound("知识库不存在")

    media_type, selected = _validate(data, settings, registry, filename, parser_profile)
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
                parser_profile=_existing_profile(session, document),
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
    build = _new_build(session, version.id, embedder_model, embedder_dim, selected)
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
        parser_profile=build.chunk_config["parser_profile"],
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
    parser_profile: str | None = None,
) -> UploadResult:
    document = doc_repo.lock_owned(session, actor, document_id)
    if expected_generation is not None and expected_generation != document.generation:
        raise VersionConflict("文档已被修改，请刷新后重试")

    media_type, selected = _validate(data, settings, registry, filename, parser_profile)
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
            parser_profile=_existing_profile(session, document, pending_first=True),
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
    build = _new_build(session, version.id, embedder_model, embedder_dim, selected)
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
        parser_profile=build.chunk_config["parser_profile"],
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
    active = session.get(IndexBuild, document.active_build_id) if document.active_build_id else None
    visible_build = active or pending

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
        # 已发布方案优先；首次处理尚未发布时仍显示冻结方案，避免失败文件隐藏选择。
        "media_type": version.media_type if version else None,
        "byte_size": version.byte_size if version else None,
        "parser_profile": (
            visible_build.chunk_config.get("resolved_parser_profile")
            or visible_build.chunk_config.get("parser_profile")
        ) if visible_build else None,
        "job": _job_dict(latest) if latest else None,
    }


INLINE_MEDIA_PREFIXES = ("application/pdf", "image/")


def source(session: Session, actor: Actor, document_id: uuid.UUID, storage: ObjectStorage) -> dict:
    """取回当前版本的原始文件，供浏览器直接预览。

    只读已发布版本的字节；所有权与状态由 `get_owned` 保证（他人与已删除一律 404）。
    是否内联展示交给调用方按媒体类型决定，这里只给出建议值。
    """
    document = doc_repo.get_owned(session, actor, document_id)
    if not document.current_version_id:
        raise NotFound("文件不存在")
    version = doc_repo.get_version(session, document.current_version_id)
    if version is None:
        raise NotFound("文件不存在")
    data = storage.get(version.object_key)
    return {
        "data": data,
        "media_type": version.media_type,
        "filename": document.title,
        "version_no": version.version_no,
        "inline": version.media_type.startswith(INLINE_MEDIA_PREFIXES),
    }


def preview_sheets(
    session: Session, actor: Actor, document_id: uuid.UUID, storage: ObjectStorage
) -> dict:
    """表格文件的只读预览数据：按工作表返回前若干行，供前端画成表格。

    这是「看原件」的替代路径——浏览器无法渲染 xlsx，转成二维数据比让用户下载更直接。
    截断信息随结果返回，前端据此提示「只显示前 N 行」。
    """
    version = _current_version(session, actor, document_id)
    return sheet_preview(storage.get(version.object_key), version.media_type)


def preview_docx(
    session: Session, actor: Actor, document_id: uuid.UUID, storage: ObjectStorage
) -> dict:
    """DOCX 的只读结构化预览：段落与表格按正文顺序返回。"""
    from knowledge.adapters.parser import DOCX

    version = _current_version(session, actor, document_id)
    if version.media_type != DOCX:
        raise UnsupportedFormat("只有 DOCX 支持文档预览")
    return docx_preview(storage.get(version.object_key))


def preview_image(
    session: Session, actor: Actor, document_id: uuid.UUID, storage: ObjectStorage
) -> dict:
    """把浏览器不支持的位图转成 PNG（目前用于 TIFF）。"""
    document = doc_repo.get_owned(session, actor, document_id)
    if not document.current_version_id:
        raise NotFound("文件不存在")
    version = doc_repo.get_version(session, document.current_version_id)
    if not version.media_type.startswith("image/"):
        raise UnsupportedFormat("只有图片文件支持图片预览")
    stem = document.title.rsplit(".", 1)[0] or "preview"
    return {
        "data": to_png(storage.get(version.object_key)),
        "media_type": "image/png",
        "filename": f"{stem}.png",
    }


def _current_version(session: Session, actor: Actor, document_id: uuid.UUID):
    document = doc_repo.get_owned(session, actor, document_id)
    if not document.current_version_id:
        raise NotFound("文件不存在")
    return doc_repo.get_version(session, document.current_version_id)


def chunks(session: Session, actor: Actor, document_id: uuid.UUID, limit: int = 200) -> dict:
    """当前已发布 build 的分块视图；未发布时返回空列表而不是报错。"""
    document = doc_repo.get_owned(session, actor, document_id)
    version = (
        doc_repo.get_version(session, document.current_version_id)
        if document.current_version_id
        else None
    )
    items: list[dict] = []
    total = 0
    if document.active_build_id:
        items = chunk_repo.list_for_document(session, actor, document.id, limit)
        total = chunk_repo.count_for_build(session, document.active_build_id)
    return {
        "document_id": str(document.id),
        "title": document.title,
        "version_no": version.version_no if version else None,
        "build_id": str(document.active_build_id) if document.active_build_id else None,
        "total": total,
        "truncated": total > len(items),
        "items": items,
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
