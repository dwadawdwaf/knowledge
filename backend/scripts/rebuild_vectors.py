"""按现行分块策略与当前向量配置重建已发布版本，并可选清理旧向量模型的构建。"""

from __future__ import annotations

import argparse
import os
import sys
import uuid
from pathlib import Path

from sqlalchemy import create_engine, delete, func, or_, select, update
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowledge.domain.entities import Actor, JobState, Lifecycle
from knowledge.domain.errors import DomainError
from knowledge.domain.ragflow_chunking import STRATEGY
from knowledge.repositories.tables import Document, DocumentVersion, IndexBuild, Job, KnowledgeBase
from knowledge.services import document as document_service


def rebuildable(model: str, dim: int, kb_id: uuid.UUID | None):
    """当前发布用的向量模型、维度或策略与目标配置不一致的文档。"""
    statement = (
        select(Document.id, KnowledgeBase.owner_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .outerjoin(IndexBuild, IndexBuild.id == Document.active_build_id)
        .where(
            Document.status == Lifecycle.ACTIVE,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.current_version_id.is_not(None),
            or_(
                IndexBuild.id.is_(None),
                IndexBuild.embedding_model != model,
                IndexBuild.embedding_dim != dim,
                IndexBuild.chunk_config["strategy"].astext.is_distinct_from(STRATEGY),
            ),
        )
        .order_by(Document.id)
    )
    if kb_id is not None:
        statement = statement.where(Document.kb_id == kb_id)
    return statement


def count_rebuildable(session: Session, model: str, dim: int, kb_id: uuid.UUID | None) -> int:
    return session.scalar(select(func.count()).select_from(rebuildable(model, dim, kb_id).subquery())) or 0


def enqueue_rebuilds(
    session: Session, model: str, dim: int, kb_id: uuid.UUID | None, limit: int | None,
    parser_profile: str | None = None,
) -> tuple[int, int]:
    """逐文档创建新构建；旧发布构建保持可检索，直到新构建发布成功。"""
    statement = rebuildable(model, dim, kb_id)
    if limit:
        statement = statement.limit(limit)
    rows = session.execute(statement).all()
    queued = skipped = 0
    for document_id, owner_id in rows:
        actor = Actor(subject_id=owner_id, tenant_id="default")
        try:
            document_service.rebuild_current_version(
                session, actor, document_id=document_id,
                embedder_model=model, embedder_dim=dim, max_attempts=3,
                parser_profile=parser_profile,
            )
            queued += 1
        except DomainError as error:
            session.rollback()
            skipped += 1
            print(f"跳过 {document_id}：{error.message}", flush=True)
    return queued, skipped


def stale_build_ids(model: str, dim: int, kb_id: uuid.UUID | None):
    scope = select(Document.id).where(Document.status == Lifecycle.ACTIVE)
    if kb_id is not None:
        scope = scope.where(Document.kb_id == kb_id)
    return select(IndexBuild.id).where(
        or_(IndexBuild.embedding_model != model, IndexBuild.embedding_dim != dim),
        IndexBuild.version_id.in_(
            select(DocumentVersion.id).where(DocumentVersion.document_id.in_(scope))
        ),
    )


def clear_other_models(session: Session, model: str, dim: int, kb_id: uuid.UUID | None) -> tuple[int, int]:
    """清空其它向量模型的构建与片段。

    全库共用一个向量索引，混用模型会让近邻检索失去意义，因此换模型必须先清空。
    只删除构建与片段：版本、原件、发布指针的记录保留，随后由重建任务重新发布。
    """
    scope = select(Document.id).where(Document.status == Lifecycle.ACTIVE)
    if kb_id is not None:
        scope = scope.where(Document.kb_id == kb_id)
    stale = select(IndexBuild.id).where(
        or_(IndexBuild.embedding_model != model, IndexBuild.embedding_dim != dim),
        IndexBuild.version_id.in_(
            select(DocumentVersion.id).where(DocumentVersion.document_id.in_(scope))
        ),
    )
    build_ids = list(session.scalars(stale))
    if not build_ids:
        return 0, 0
    # 先删目标已不存在的排队/运行任务，再解除文档与已完成任务对旧构建的引用，
    # 最后删构建（片段与词项级联删除）。
    session.execute(
        delete(Job).where(
            Job.build_id.in_(build_ids), Job.state.in_([JobState.QUEUED, JobState.RUNNING])
        )
    )
    session.execute(
        update(Document).where(Document.active_build_id.in_(build_ids)).values(active_build_id=None)
    )
    session.execute(
        update(Document).where(Document.pending_build_id.in_(build_ids)).values(pending_build_id=None)
    )
    session.execute(
        update(Job)
        .where(Job.build_id.in_(build_ids), Job.state.not_in([JobState.QUEUED, JobState.RUNNING]))
        .values(build_id=None, generation=None)
    )
    session.execute(delete(IndexBuild).where(IndexBuild.id.in_(build_ids)))
    session.commit()
    return len(build_ids), sum(1 for _ in build_ids)


def purgeable(model: str, dim: int, kb_id: uuid.UUID | None):
    """只清理已用目标配置发布的文档名下的退役构建，现役与待发布构建一律不碰。"""
    rebuilt_documents = (
        select(Document.id)
        .join(IndexBuild, IndexBuild.id == Document.active_build_id)
        .where(
            Document.status == Lifecycle.ACTIVE,
            IndexBuild.embedding_model == model,
            IndexBuild.embedding_dim == dim,
            IndexBuild.chunk_config["strategy"].astext == STRATEGY,
        )
    )
    if kb_id is not None:
        rebuilt_documents = rebuilt_documents.where(Document.kb_id == kb_id)
    in_use = select(Document.active_build_id).where(Document.active_build_id.is_not(None))
    pending = select(Document.pending_build_id).where(Document.pending_build_id.is_not(None))
    return select(IndexBuild.id).where(
        IndexBuild.version_id.in_(
            select(DocumentVersion.id).where(DocumentVersion.document_id.in_(rebuilt_documents))
        ),
        IndexBuild.id.not_in(in_use),
        IndexBuild.id.not_in(pending),
    )


def purge_old_builds(session: Session, model: str, dim: int, kb_id: uuid.UUID | None) -> int:
    build_ids = list(session.scalars(purgeable(model, dim, kb_id)))
    if not build_ids:
        return 0
    # 任务保留为审计记录，只解除与已删除构建的引用；片段与词项随构建级联删除。
    session.execute(
        update(Job)
        .where(Job.build_id.in_(build_ids), Job.state.not_in([JobState.QUEUED, JobState.RUNNING]))
        .values(build_id=None, generation=None)
    )
    session.execute(delete(IndexBuild).where(IndexBuild.id.in_(build_ids)))
    session.commit()
    return len(build_ids)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="真正创建重建任务；默认只统计")
    parser.add_argument("--purge-old", action="store_true", help="清理已重建文档的退役构建与片段")
    parser.add_argument(
        "--clear-old", action="store_true",
        help="先清空其它向量模型的构建与片段（全库禁止混用模型时必须先做）",
    )
    parser.add_argument("--limit", type=int, default=None, help="本轮最多提交多少个文档")
    parser.add_argument("--kb", type=str, default=None, help="只处理指定知识库")
    parser.add_argument("--parser-profile", default=None, help="覆盖解析方案，例如扫描件用 pdf-auto-v1")
    parser.add_argument("--embedding-model", default=os.environ.get("KS_EMBEDDING_MODEL", ""))
    parser.add_argument("--embedding-dim", type=int, default=int(os.environ.get("KS_EMBEDDING_DIM") or 0))
    args = parser.parse_args()

    url = os.environ.get("KS_DATABASE_URL")
    if not url:
        parser.error("须显式设置 KS_DATABASE_URL")
    if not args.embedding_model or not args.embedding_dim:
        parser.error("须显式指定目标向量模型与维度")
    if (args.clear_old or args.purge_old) and not args.apply:
        parser.error("--clear-old 与 --purge-old 是不可逆删除，必须与 --apply 同时使用")
    kb_id = uuid.UUID(args.kb) if args.kb else None

    engine = create_engine(url, hide_parameters=True)
    try:
        with Session(engine) as session:
            pending = count_rebuildable(session, args.embedding_model, args.embedding_dim, kb_id)
            print(f"需要按 {args.embedding_model}（{args.embedding_dim} 维）重建的文档：{pending}")
            if args.clear_old:
                stale = list(session.scalars(stale_build_ids(
                    args.embedding_model, args.embedding_dim, kb_id)))
                preview = ", ".join(str(item) for item in stale[:10])
                print(f"将清空其它向量模型的构建 {len(stale)} 个" + (f"（示例：{preview}）" if stale else ""))
                cleared, _ = clear_other_models(session, args.embedding_model, args.embedding_dim, kb_id)
                print(f"已清空其它向量模型的构建：{cleared}")
                pending = count_rebuildable(session, args.embedding_model, args.embedding_dim, kb_id)
            submitted = 0
            if args.apply and pending:
                submitted, skipped = enqueue_rebuilds(
                    session, args.embedding_model, args.embedding_dim, kb_id, args.limit,
                    args.parser_profile,
                )
                print(f"已提交重建任务：{submitted}；跳过：{skipped}")
            if args.purge_old:
                purged = purge_old_builds(session, args.embedding_model, args.embedding_dim, kb_id)
                print(f"已清理退役构建：{purged}")
            remaining = count_rebuildable(session, args.embedding_model, args.embedding_dim, kb_id)
            print(f"仍待重建的文档：{remaining}")
            # 已提交重建时剩余数只说明 Worker 尚未跑完，不算失败；没有任何进展才算失败。
            return int(bool(remaining) and not submitted)
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())
