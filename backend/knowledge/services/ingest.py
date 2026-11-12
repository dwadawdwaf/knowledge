"""索引构建：解析 → 分段 → 向量化 → 原子发布。

每个写入阶段前校验租约；发布在单事务内完成栅栏校验与指针切换。
"""

from __future__ import annotations

import math
import time
import uuid

from sqlalchemy.orm import Session

from knowledge.adapters.ragflow_text import count_tokens, search_fields
from knowledge.config import Settings
from knowledge.domain.entities import BuildState, JobState
from knowledge.domain.errors import (
    DomainError,
    ModelTimeout,
    ModelTransientUnavailable,
    ModelUnavailable,
    ParseFailed,
    StaleJob,
)
from knowledge.domain.ports import Embedder, ObjectStorage
from knowledge.domain.ragflow_chunking import STRATEGY, chunk_ragflow
from knowledge.logging import get_logger
from knowledge.repositories import builds as build_repo
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import EMBEDDING_DIM

log = get_logger("ingest")


class EmptyContent(DomainError):
    code = "EMPTY_CONTENT"
    http_status = 422


def run(
    session: Session,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    document_id: uuid.UUID,
    settings: Settings,
    storage: ObjectStorage,
    registry,
    embedder: Embedder,
) -> None:
    try:
        document, _job, build, version = build_repo.lock_execution(session, job_id, lease_token)
    except StaleJob:
        session.rollback()
        job_repo.finish(session, job_id, lease_token, JobState.CANCELLED, "cancelled")
        return
    if document.id != document_id:
        raise StaleJob("任务目标不匹配")
    if build.embedding_model != embedder.model or build.embedding_dim != embedder.dimension:
        raise ModelUnavailable("构建向量配置已变化，请按原配置处理或重建")
    generation = document.generation
    build_id, version_id, title = build.id, version.id, document.title
    object_key, media_type = version.object_key, version.media_type
    chunk_config = dict(build.chunk_config)
    strategy = chunk_config.get("strategy")
    if strategy != STRATEGY:
        raise ParseFailed("该任务使用已停用的分块策略，请上传新版本")
    build.state = BuildState.RUNNING
    session.commit()

    stage_marks: list[tuple[str, float]] = []

    def _enter_stage(stage: str) -> None:
        """记录上一阶段耗时并开启新阶段；阶段结束由下一阶段或收尾时落账。"""
        now = time.monotonic()
        if stage_marks:
            name, started_at = stage_marks[-1]
            log.info(
                "stage_done",
                extra={
                    "job_id": str(job_id),
                    "build_id": str(build_id),
                    "stage": name,
                    "duration_ms": round((now - started_at) * 1000, 1),
                },
            )
        stage_marks.append((stage, now))

    # 1) 解析
    if not job_repo.set_stage(session, job_id, lease_token, "parsing"):
        raise DomainError("租约已失效")
    _enter_stage("parsing")
    data = storage.get(object_key)
    try:
        profile = chunk_config.get("resolved_parser_profile", chunk_config.get("parser_profile", "legacy-v1"))
        blocks = []
        if profile == "pdf-auto-v1":
            blocks = registry.probe_pdf(data, title)
            profile = "legacy-v1" if blocks else "mineru-api4.1-v1"
            _, _, routed_build, _ = build_repo.lock_execution(session, job_id, lease_token)
            if routed_build.id != build_id:
                raise StaleJob("任务构建已变化")
            routed_build.chunk_config = {**chunk_config, "resolved_parser_profile": profile}
            session.commit()
        if not blocks:
            parser = registry.for_file(media_type, title, profile=profile, structured_markdown=strategy == STRATEGY)
            blocks = parser.parse(data, media_type, title)
    except DomainError:
        raise
    except Exception as exc:  # 解析器内部异常统一归为失败
        raise ParseFailed(f"解析失败：{type(exc).__name__}") from exc

    if not blocks:
        raise EmptyContent("未提取到正文")

    # 2) 分段
    if not job_repo.set_stage(session, job_id, lease_token, "chunking"):
        raise DomainError("租约已失效")
    _enter_stage("chunking")
    pieces = chunk_ragflow(
        blocks, count_tokens, mineru_pdf=media_type == "application/pdf" and profile == "mineru-api4.1-v1",
    )
    if not pieces:
        raise EmptyContent("分段结果为空")

    if not job_repo.set_stage(session, job_id, lease_token, "enhancing"):
        raise StaleJob("任务执行资格已失效")
    _enter_stage("enhancing")
    terms = [
        search_fields(
            piece.text, title, piece.structure["section_path"], piece.structure["table_header"],
            row_fields=piece.structure["table_fields"],
        )
        for piece in pieces
    ]

    # 3) 向量化
    if not job_repo.set_stage(session, job_id, lease_token, "embedding"):
        raise DomainError("租约已失效")
    _enter_stage("embedding")
    texts = [piece.text for piece in pieces]
    try:
        vectors = embedder.embed(texts)
    except (ModelTimeout, ModelTransientUnavailable) as exc:
        reason = {
            "向量服务超时": "timeout",
            "向量服务连接被拒绝": "connection_refused",
            "向量服务名称解析失败": "dns_failure",
        }.get(exc.message, "upstream_unavailable")
        log.warning("embedding_retry", extra={
            "request_id": f"job:{job_id}", "job_id": str(job_id),
            "build_id": str(build_id), "error_code": exc.code,
            "reason": reason,
        })
        time.sleep(2)
        if not job_repo.set_stage(session, job_id, lease_token, "embedding"):
            raise StaleJob("任务执行资格已失效") from exc
        vectors = embedder.embed(texts)
    if len(vectors) != len(pieces):
        raise ModelUnavailable("向量数量与分块数量不一致")
    for vector in vectors:
        if len(vector) != EMBEDDING_DIM:
            raise ModelUnavailable(f"向量维度不符：期望 {EMBEDDING_DIM}，实际 {len(vector)}")
        if not all(map(math.isfinite, vector)) or not any(vector):
            raise ModelUnavailable("分块向量无效")

    # 写入与发布共用短事务；重试使用新 build，不删除任何已有分块。
    _, _, locked_build, _ = build_repo.lock_execution(session, job_id, lease_token)
    if locked_build.id != build_id:
        raise StaleJob("任务构建已变化")
    locked_build.warnings = sorted({warning for piece in pieces for warning in piece.warnings})
    build_repo.guard_vector_index(session, job_id, lease_token)
    chunk_repo.insert_many(
        session,
        build_id,
        [
            {
                "ordinal": piece.ordinal,
                "text": piece.text,
                "locator": piece.locator,
                "warnings": piece.warnings,
                "embedding": vector,
                "tsv": piece.text,
                "structure": piece.structure,
                "search_terms": terms[index],
            }
            for index, (piece, vector) in enumerate(zip(pieces, vectors, strict=True))
        ],
    )
    _enter_stage("publishing")

    if settings.search_engine == "opensearch":
        from knowledge.domain.entities import Actor
        from knowledge.repositories.tables import KnowledgeBase
        from knowledge.services.native_search import synchronize

        owner = session.get(KnowledgeBase, document.kb_id)
        if owner is None:
            raise StaleJob("知识库已不存在")
        actor = Actor(owner.owner_id, "worker")
        # 分块先持久化但尚未发布；不在锁内调用搜索引擎。发布时再次核验租约。
        session.commit()
        synchronize(session, actor, settings, [build_id])

    # 5) 原子发布
    published = publish(
        session,
        job_id=job_id,
        lease_token=lease_token,
        document_id=document_id,
        build_id=build_id,
        version_id=version_id,
        generation=generation,
    )
    _enter_stage("done")
    if published:
        log.info(
            "build_published",
            extra={
                "job_id": str(job_id),
                "build_id": str(build_id),
                "document_id": str(document_id),
                "chunk_count": len(pieces),
            },
        )
    else:
        log.warning(
            "build_publish_rejected",
            extra={
                "job_id": str(job_id),
                "build_id": str(build_id),
                "document_id": str(document_id),
            },
        )


def publish(
    session: Session,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    document_id: uuid.UUID,
    build_id: uuid.UUID,
    version_id: uuid.UUID,
    generation: int,
) -> bool:
    try:
        build_repo.publish(
            session,
            job_id=job_id,
            token=lease_token,
            document_id=document_id,
            build_id=build_id,
            version_id=version_id,
            generation=generation,
        )
        session.commit()
    except Exception:
        session.rollback()
        raise
    return True
