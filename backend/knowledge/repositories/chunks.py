"""分块检索与读取。授权条件写在两路召回的子查询内部。"""

from __future__ import annotations

import uuid
from collections.abc import Sequence
from typing import Any, cast

from sqlalchemy import Result, bindparam, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, Lifecycle, SearchHit
from knowledge.domain.errors import NotFound
from knowledge.repositories.tables import (
    Chunk,
    Document,
    DocumentVersion,
    IndexBuild,
    KnowledgeBase,
)


def _affected(result: Result[Any]) -> int:
    # text() 删除在运行期返回 CursorResult；Result 类型上没有 rowcount
    return cast(CursorResult[Any], result).rowcount

_FILTERS = """
        kb.owner_id = :owner
        AND kb.status = 'active'
        AND d.status = 'active'
        AND b.state = 'ready'
        AND d.kb_id IN :kb_ids
"""

_KW_CTE = f"""
    kw AS (
        SELECT c.id AS id,
               row_number() OVER (ORDER BY similarity(c.tsv, :q) DESC, c.ordinal) AS rnk
        FROM chunks c
        JOIN index_builds b ON b.id = c.build_id
        JOIN documents d ON d.active_build_id = c.build_id
        JOIN knowledge_bases kb ON kb.id = d.kb_id
        WHERE {_FILTERS}
          AND similarity(c.tsv, :q) >= :floor
        LIMIT :recall
    )
"""

_SEM_CTE = f"""
    sem AS (
        SELECT c.id AS id,
               row_number() OVER (ORDER BY c.embedding <=> CAST(:vec AS vector)) AS rnk
        FROM chunks c
        JOIN index_builds b ON b.id = c.build_id
        JOIN documents d ON d.active_build_id = c.build_id
        JOIN knowledge_bases kb ON kb.id = d.kb_id
        WHERE {_FILTERS}
          AND c.embedding <=> CAST(:vec AS vector) < :max_distance
        LIMIT :recall
    )
"""


def _build_sql(mode: str) -> str:
    where = ""
    if mode == "keyword":
        ctes = f"WITH {_KW_CTE}"
        joins = "JOIN kw ON kw.id = c.id"
        score = "1.0 / (:k + kw.rnk)"
    elif mode == "semantic":
        ctes = f"WITH {_SEM_CTE}"
        joins = "JOIN sem ON sem.id = c.id"
        score = "1.0 / (:k + sem.rnk)"
    else:
        ctes = f"WITH {_KW_CTE}, {_SEM_CTE}"
        joins = "LEFT JOIN kw ON kw.id = c.id LEFT JOIN sem ON sem.id = c.id"
        # 两路都没命中的分块必须排除，否则 LEFT JOIN 会把全表带出来
        where = "WHERE kw.id IS NOT NULL OR sem.id IS NOT NULL"
        score = (
            "COALESCE(1.0 / (:k + kw.rnk), 0) + COALESCE(1.0 / (:k + sem.rnk), 0)"
        )

    return f"""
    {ctes}
    SELECT c.id AS chunk_id, c.build_id, b.version_id, d.id AS document_id,
           d.title, c.text, c.locator, {score} AS score
    FROM chunks c
    JOIN index_builds b ON b.id = c.build_id
    JOIN documents d ON d.active_build_id = c.build_id
    {joins}
    {where}
    ORDER BY score DESC
    LIMIT :top_k
    """


def search(
    session: Session,
    actor: Actor,
    *,
    kb_ids: Sequence[uuid.UUID],
    query: str,
    mode: str,
    vector: Sequence[float] | None,
    top_k: int,
    recall_limit: int,
    rrf_k: int,
    similarity_floor: float,
    max_distance: float,
) -> list[SearchHit]:
    if mode in ("semantic", "hybrid") and vector is None:
        raise ValueError("该模式需要查询向量")

    statement = text(_build_sql(mode)).bindparams(bindparam("kb_ids", expanding=True))
    params = {
        "owner": actor.subject_id,
        "kb_ids": list(kb_ids),
        "q": query,
        "floor": similarity_floor,
        "recall": recall_limit,
        "top_k": top_k,
        "k": rrf_k,
        "max_distance": max_distance,
    }
    if vector is not None:
        params["vec"] = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"

    rows = session.execute(statement, params).mappings().all()
    return [
        SearchHit(
            document_id=str(row["document_id"]),
            version_id=str(row["version_id"]),
            build_id=str(row["build_id"]),
            chunk_id=str(row["chunk_id"]),
            title=row["title"],
            text=row["text"],
            locator=row["locator"],
            score=float(row["score"]),
            score_type="rrf" if mode != "semantic" else "cosine_rank",
        )
        for row in rows
    ]


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
            embedding=row["embedding"],
            tsv=row["tsv"],
        )
        for row in rows
    ]
    session.add_all(objects)
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
    }


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
