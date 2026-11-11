"""分块检索与读取。授权条件写在两路召回的子查询内部。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import Result, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, Lifecycle
from knowledge.domain.errors import ModelUnavailable, NotFound
from knowledge.domain.ragflow_query import LEXICAL_REVISION
from knowledge.repositories.tables import (
    Chunk,
    ChunkSearchTerms,
    Document,
    DocumentVersion,
    IndexBuild,
    KnowledgeBase,
)


def _affected(result: Result[Any]) -> int:
    # text() 删除在运行期返回 CursorResult；Result 类型上没有 rowcount
    return cast(CursorResult[Any], result).rowcount


def assert_compatible(
    session: Session,
    actor: Actor,
    kb_ids: Sequence[uuid.UUID],
    model: str | None,
    dimension: int | None,
    *,
    document_ids: Sequence[uuid.UUID] = (),
) -> None:
    statement = (
        select(IndexBuild.id)
        .join(Document, Document.active_build_id == IndexBuild.id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            KnowledgeBase.owner_id == actor.subject_id,
            KnowledgeBase.status == Lifecycle.ACTIVE,
            Document.status == Lifecycle.ACTIVE,
            Document.kb_id.in_(kb_ids),
            IndexBuild.state == "ready",
            (IndexBuild.embedding_model != model) | (IndexBuild.embedding_dim != dimension),
        )
    )
    if document_ids:
        statement = statement.where(Document.id.in_(document_ids))
    incompatible = session.scalar(statement.limit(1))
    if incompatible is not None:
        raise ModelUnavailable("索引与查询向量配置不兼容，请恢复原配置或重建索引")


def insert_many(
    session: Session,
    build_id: uuid.UUID,
    rows: Sequence[dict],
) -> int:
    objects = [
        Chunk(
            build_id=build_id,
            ordinal=row["ordinal"],
            text=row["text"],
            locator=row["locator"],
            warnings=list(row.get("warnings") or ()),
            embedding=row["embedding"],
            tsv=row["tsv"],
            structure=row.get("structure", {}),
        )
        for row in rows
    ]
    session.add_all(objects)
    session.flush()
    session.add_all([
        ChunkSearchTerms(
            chunk_id=chunk.id, terms=row["search_terms"].terms,
            boosted=row["search_terms"].boosted, fields=row["search_terms"].fields,
            lexical_revision=LEXICAL_REVISION,
        )
        for chunk, row in zip(objects, rows, strict=True) if row.get("search_terms") is not None
    ])
    session.flush()
    return len(objects)


def get_owned(session: Session, actor: Actor, chunk_id: uuid.UUID) -> dict:
    stmt = (
        select(
            Chunk.id,
            Chunk.text,
            Chunk.locator,
            Chunk.build_id,
            IndexBuild.version_id,
            Document.id,
            Document.title,
            IndexBuild.warnings,
        )
        .join(IndexBuild, IndexBuild.id == Chunk.build_id)
        .join(DocumentVersion, DocumentVersion.id == IndexBuild.version_id)
        .join(Document, Document.id == DocumentVersion.document_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            Chunk.id == chunk_id,
            KnowledgeBase.owner_id == actor.subject_id,
            Document.status == Lifecycle.ACTIVE,
        )
    )
    row = session.execute(stmt).first()
    if row is None:
        raise NotFound("分块不存在")
    return {
        "chunk_id": str(row[0]),
        "text": row[1],
        "locator": row[2],
        "build_id": str(row[3]),
        "version_id": str(row[4]),
        "document_id": str(row[5]),
        "title": row[6],
        "warnings": row[7] or [],
    }


def list_for_document(
    session: Session, actor: Actor, document_id: uuid.UUID, limit: int
) -> list[dict]:
    """按序返回当前已发布 build 的分块，用于前端查看分段结果。"""
    stmt = (
        select(Chunk.id, Chunk.ordinal, Chunk.text, Chunk.locator, Chunk.build_id, Chunk.warnings)
        .join(Document, Document.active_build_id == Chunk.build_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(
            Document.id == document_id,
            KnowledgeBase.owner_id == actor.subject_id,
            Document.status == Lifecycle.ACTIVE,
        )
        .order_by(Chunk.ordinal)
        .limit(limit)
    )
    return [
        {
            "chunk_id": str(row[0]),
            "ordinal": row[1],
            "text": row[2],
            "locator": row[3],
            "build_id": str(row[4]),
            "warnings": list(row[5] or ()),
        }
        for row in session.execute(stmt)
    ]


def delete_for_document(session: Session, document_id: uuid.UUID) -> int:
    result = session.execute(
        text(
            """
            DELETE FROM chunks WHERE build_id IN (
                SELECT b.id FROM index_builds b
                JOIN document_versions v ON v.id = b.version_id
                WHERE v.document_id = :doc
            )
            """
        ),
        {"doc": document_id},
    )
    session.flush()
    return _affected(result)


def count_for_build(session: Session, build_id: uuid.UUID) -> int:
    stmt = select(func.count()).select_from(Chunk).where(Chunk.build_id == build_id)
    return int(session.scalar(stmt) or 0)
