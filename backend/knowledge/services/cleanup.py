"""清理用例：删除原件、分块、构建与版本，最后标记文档已删除。"""

from __future__ import annotations

import uuid

from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.entities import JobState, Lifecycle
from knowledge.domain.ports import ObjectStorage
from knowledge.logging import get_logger
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import documents as doc_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import Document, DocumentVersion, IndexBuild
from knowledge.services import knowledge as knowledge_service

log = get_logger("services.cleanup")

# 超过这个数量的孤儿不再按「零星遗留」处理（见 reclaim_orphans 的说明）。
ORPHAN_BATCH_LIMIT = 20


def looks_like_foreign_volume(candidate_count: int, object_count: int) -> bool:
    """一次要删的对象既多又占大头：更像卷接错了库，而不是零星的失败上传遗留。"""
    return candidate_count > ORPHAN_BATCH_LIMIT and candidate_count * 2 > object_count


def reclaim_orphans(session: Session, storage: ObjectStorage, max_age_seconds: float) -> int:
    """回收超过 max_age 仍无版本引用的对象（上传后事务失败遗留），返回回收数。

    孤儿应当是零星的：一次要删掉大半对象，通常是「卷指向了另一个库的数据」
    这类配置错误，而不是真的遗留物。这种情况下宁可跳过并留下日志，也不整卷清空。
    """
    referenced = {row[0] for row in session.execute(select(DocumentVersion.object_key))}
    objects = list(storage.iter_objects())
    candidates = [
        object_key for object_key, age in objects if age >= max_age_seconds
        and object_key not in referenced
    ]
    if looks_like_foreign_volume(len(candidates), len(objects)):
        log.warning(
            "orphan_reclaim_skipped",
            extra={
                "candidates": len(candidates),
                "objects": len(objects),
                "reason": "对象卷与当前数据库不匹配，疑似配置错误",
            },
        )
        return 0
    for object_key in candidates:
        storage.delete(object_key)
    return len(candidates)


def run(
    session: Session,
    *,
    job_id: uuid.UUID,
    lease_token: uuid.UUID,
    document_id: uuid.UUID,
    storage: ObjectStorage,
    settings: Settings | None = None,
) -> None:
    document = session.get(Document, document_id)
    if document is None:
        job_repo.finish(session, job_id, lease_token, JobState.SUCCEEDED, "cleaning")
        return

    versions = doc_repo.versions_of(session, document_id)
    build_ids = list(session.scalars(select(IndexBuild.id).where(
        IndexBuild.version_id.in_([version.id for version in versions]),
    )))
    object_keys = [version.object_key for version in versions]

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

    # 提交即完成授权阻断（C7）。原件与搜索索引随后删除：本地删除是幂等的，
    # 失败遗留对象会由孤儿回收按 max_age 兜底；搜索索引失败只记告警。
    if settings is not None and settings.search_engine == "opensearch":
        try:
            from knowledge.adapters.ragflow_opensearch import NativeIndex
            NativeIndex(settings).delete(build_ids)
        except Exception as exc:  # noqa: BLE001 - 索引数据可重建，不阻断清理收尾
            log.warning("opensearch_cleanup_failed", extra={
                "document_id": str(document_id), "builds": len(build_ids),
                "error": type(exc).__name__,
            })
    for object_key in object_keys:
        storage.delete(object_key)
    job_repo.finish(session, job_id, lease_token, JobState.SUCCEEDED, "cleaning")
