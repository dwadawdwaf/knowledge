"""索引构建：解析 → 分段 → 向量化 → 原子发布。

每个写入阶段前校验租约；发布在单事务内完成栅栏校验与指针切换。
"""

from __future__ import annotations

import time
import uuid

from sqlalchemy import text
from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.chunking import chunk_blocks
from knowledge.domain.entities import JobState, Lifecycle
from knowledge.domain.errors import DomainError, ModelUnavailable
from knowledge.domain.ports import Embedder, ObjectStorage
from knowledge.logging import get_logger
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import documents as doc_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import EMBEDDING_DIM, Document, IndexBuild

log = get_logger("ingest")


class EmptyContent(DomainError):
    code = "EMPTY_CONTENT"
    http_status = 422


class ParseFailed(DomainError):
    code = "PARSE_FAILED"
    http_status = 422


def _holds_lease(session: Session, job_id: uuid.UUID, token: uuid.UUID) -> bool:
    row = session.execute(
        text("SELECT 1 FROM jobs WHERE id = :id AND lease_token = :token AND state = 'running'"),
        {"id": job_id, "token": token},
    ).first()
    return row is not None


def _ensure_lease(session: Session, job_id: uuid.UUID, token: uuid.UUID) -> None:
    if not _holds_lease(session, job_id, token):
        raise DomainError("租约已失效")


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
    document = session.get(Document, document_id)
    if document is None or document.status != Lifecycle.ACTIVE:
        job_repo.finish(session, job_id, lease_token, JobState.CANCELLED, "cancelled")
        return

    if document.pending_build_id is None:
        job_repo.finish(session, job_id, lease_token, JobState.CANCELLED, "cancelled")
        return

    build = session.get(IndexBuild, document.pending_build_id)
    if build is None:
        job_repo.finish(session, job_id, lease_token, JobState.CANCELLED, "cancelled")
        return

    generation = document.generation
    version = doc_repo.get_version(session, build.version_id)

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
                    "build_id": str(build.id),
                    "stage": name,
                    "duration_ms": round((now - started_at) * 1000, 1),
                },
            )
        stage_marks.append((stage, now))

    # 1) 解析
    if not job_repo.set_stage(session, job_id, lease_token, "parsing"):
        raise DomainError("租约已失效")
    _enter_stage("parsing")
    data = storage.get(version.object_key)
    try:
        blocks = registry.for_file(version.media_type, document.title).parse(
            data, version.media_type, document.title
        )
    except DomainError:
        raise
    except Exception as exc:  # 解析器内部异常统一归为失败
        raise ParseFailed(f"解析失败：{type(exc).__name__}") from exc

    if not blocks or not any(block.text.strip() for block in blocks):
        raise EmptyContent("未提取到正文")

    # 2) 分段
    if not job_repo.set_stage(session, job_id, lease_token, "chunking"):
        raise DomainError("租约已失效")
    _enter_stage("chunking")
    pieces = chunk_blocks(blocks, settings.chunk_max_chars, settings.chunk_overlap_chars)
    if not pieces:
        raise EmptyContent("分段结果为空")

    # 3) 向量化
    if not job_repo.set_stage(session, job_id, lease_token, "embedding"):
        raise DomainError("租约已失效")
    _enter_stage("embedding")
    vectors = embedder.embed([piece.text for piece in pieces])
    if len(vectors) != len(pieces):
        raise ModelUnavailable("向量数量与分块数量不一致")
    for vector in vectors:
        if len(vector) != EMBEDDING_DIM:
            raise ModelUnavailable(
                f"向量维度不符：期望 {EMBEDDING_DIM}，实际 {len(vector)}"
            )

    # 4) 写分块（先清理本次构建的残留，保证重试幂等）
    _ensure_lease(session, job_id, lease_token)
    session.execute(text("DELETE FROM chunks WHERE build_id = :build"), {"build": build.id})
    chunk_repo.insert_many(
        session,
        build.id,
        [
            {
                "ordinal": piece.ordinal,
                "text": piece.text,
                "locator": piece.locator,
                "embedding": vector,
                "tsv": piece.text,
            }
            for piece, vector in zip(pieces, vectors)
        ],
    )
    build.embedding_model = embedder.model
    build.embedding_dim = EMBEDDING_DIM
    session.commit()
    _enter_stage("publishing")

    # 5) 原子发布
    published = publish(
        session,
        job_id=job_id,
        lease_token=lease_token,
        document_id=document.id,
        build_id=build.id,
        version_id=version.id,
        generation=generation,
    )
    _enter_stage("done")
    if published:
        log.info(
            "build_published",
            extra={
                "job_id": str(job_id),
                "build_id": str(build.id),
                "document_id": str(document.id),
                "chunk_count": len(pieces),
            },
        )
    else:
        log.warning(
            "build_publish_rejected",
            extra={"job_id": str(job_id), "build_id": str(build.id), "document_id": str(document.id)},
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
    row = session.execute(
        text(
            "SELECT generation, pending_build_id, active_build_id, status"
            " FROM documents WHERE id = :id FOR UPDATE"
        ),
        {"id": document_id},
    ).mappings().first()

    if row is None:
        job_repo.finish(session, job_id, lease_token, JobState.CANCELLED, "cancelled")
        return False

    if (
        row["status"] != Lifecycle.ACTIVE.value
        or row["pending_build_id"] != build_id
        or row["generation"] != generation
        or not _holds_lease(session, job_id, lease_token)
    ):
        session.rollback()
        job_repo.finish(session, job_id, lease_token, JobState.CANCELLED, "cancelled")
        return False

    session.execute(
        text(
            "UPDATE documents SET current_version_id = :version, active_build_id = :build,"
            " pending_build_id = NULL, updated_at = now() WHERE id = :id"
        ),
        {"version": version_id, "build": build_id, "id": document_id},
    )
    session.execute(
        text("UPDATE index_builds SET state = 'ready' WHERE id = :build"), {"build": build_id}
    )
    if row["active_build_id"] is not None:
        session.execute(
            text("UPDATE index_builds SET state = 'retired' WHERE id = :old AND state = 'ready'"),
            {"old": row["active_build_id"]},
        )

    session.commit()
    job_repo.finish(session, job_id, lease_token, JobState.SUCCEEDED, "publishing")
    return True


def mark_build_failed(session: Session, document_id: uuid.UUID, build_id: uuid.UUID) -> None:
    session.execute(
        text("UPDATE index_builds SET state = 'failed' WHERE id = :build AND state <> 'ready'"),
        {"build": build_id},
    )
    session.commit()
