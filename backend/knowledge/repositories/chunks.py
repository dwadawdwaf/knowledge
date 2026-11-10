"""分块检索与读取。授权条件写在两路召回的子查询内部。"""

from __future__ import annotations

import uuid
from collections.abc import Iterator, Sequence
from typing import Any, cast

from sqlalchemy import Result, bindparam, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, Lifecycle, SearchHit
from knowledge.domain.errors import ModelUnavailable, NotFound
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

def _keyword_cte(name: str, score: str, matches: str) -> str:
    return f"""
    {name} AS (
        SELECT c.id AS id,
               {score} AS raw_score,
               row_number() OVER (ORDER BY {score} DESC, c.ordinal, c.id) AS rnk
        FROM chunks c
        JOIN index_builds b ON b.id = c.build_id
        JOIN documents d ON d.active_build_id = c.build_id
        JOIN knowledge_bases kb ON kb.id = d.kb_id
        WHERE {_FILTERS}
          AND {matches}
        ORDER BY {score} DESC, c.ordinal, c.id
        LIMIT :recall
    )
"""

_SEM_CTE = f"""
    sem AS (
        SELECT c.id AS id,
               c.embedding <=> CAST(:vec AS vector) AS distance,
               row_number() OVER (ORDER BY c.embedding <=> CAST(:vec AS vector), c.ordinal, c.id) AS rnk
        FROM chunks c
        JOIN index_builds b ON b.id = c.build_id
        JOIN documents d ON d.active_build_id = c.build_id
        JOIN knowledge_bases kb ON kb.id = d.kb_id
        WHERE {_FILTERS}
          AND b.embedding_model = :model AND b.embedding_dim = :dimension
          AND c.embedding <=> CAST(:vec AS vector) < :max_distance
        ORDER BY c.embedding <=> CAST(:vec AS vector), c.ordinal, c.id
        LIMIT :recall
    )
"""


_FTS_QUERY_CTE = """
    fts_query AS (
        SELECT string_agg(quote_literal(term), ' | ' ORDER BY term)::tsquery AS query
        FROM unnest(tsvector_to_array(to_tsvector('pg_catalog.english'::regconfig, :q))) AS terms(term)
    )
"""
_FTS_VECTOR = "to_tsvector('pg_catalog.english'::regconfig, c.tsv)"


_BM25_CTE = f"""
    kw AS (
        SELECT c.id, candidate.raw_score, row_number() OVER (ORDER BY candidate.rank) AS rnk
        FROM unnest(CAST(:bm25_ids AS uuid[]), CAST(:bm25_scores AS double precision[]))
            WITH ORDINALITY AS candidate(id, raw_score, rank)
        JOIN chunks c ON c.id = candidate.id
        JOIN index_builds b ON b.id = c.build_id
        JOIN documents d ON d.active_build_id = c.build_id
        JOIN knowledge_bases kb ON kb.id = d.kb_id
        WHERE {_FILTERS}
    )
"""


def _build_sql(
    mode: str, optimized: bool = False, full_text: bool = False, bm25: bool = False,
) -> str:
    quality = "similarity(c.tsv, :q)"
    matches = f"{quality} >= :floor"
    if optimized:
        exact = """EXISTS (
            SELECT 1 FROM unnest(CAST(:terms AS text[])) AS terms(term)
            WHERE strpos(lower(c.tsv), lower(term)) > 0
        )"""
        quality = f"CASE WHEN {exact} THEN 1.0 ELSE GREATEST(similarity(c.tsv, :q), word_similarity(:q, c.tsv)) END"
        matches = f"(similarity(c.tsv, :q) >= :floor OR word_similarity(:q, c.tsv) >= :local_floor OR {exact})"
    kw_cte = _keyword_cte("kw", quality, matches)
    if full_text and mode != "semantic":
        # 词项由 PostgreSQL 规范化和转义。空词项与普通英文分别走互斥分支。
        query = "(SELECT query FROM fts_query)"
        fts_cte = _keyword_cte("fts_kw", f"ts_rank_cd({_FTS_VECTOR}, {query})", f"{_FTS_VECTOR} @@ {query}")
        fallback_cte = _keyword_cte("fallback_kw", quality, f"{query} IS NULL AND {matches}")
        kw_cte = f"""
        {_FTS_QUERY_CTE}, {fts_cte}, {fallback_cte},
        kw AS (SELECT * FROM fts_kw UNION ALL SELECT * FROM fallback_kw)
        """
    if bm25:
        kw_cte = _BM25_CTE
    where = ""
    if mode == "keyword":
        ctes = f"WITH {kw_cte}"
        joins = "JOIN kw ON kw.id = c.id"
        score = "kw.raw_score" if bm25 else "1.0 / (:k + kw.rnk)"
        details = "kw.raw_score AS keyword_score, kw.rnk AS keyword_rank, NULL AS semantic_distance, NULL AS semantic_rank"
    elif mode == "semantic":
        ctes = f"WITH {_SEM_CTE}"
        joins = "JOIN sem ON sem.id = c.id"
        score = "1.0 / (:k + sem.rnk)"
        details = "NULL AS keyword_score, NULL AS keyword_rank, sem.distance AS semantic_distance, sem.rnk AS semantic_rank"
    else:
        ctes = f"WITH {kw_cte}, {_SEM_CTE}"
        joins = "LEFT JOIN kw ON kw.id = c.id LEFT JOIN sem ON sem.id = c.id"
        # 两路都没命中的分块必须排除，否则 LEFT JOIN 会把全表带出来
        where = "WHERE kw.id IS NOT NULL OR sem.id IS NOT NULL"
        score = "COALESCE(:kw_weight / (:k + kw.rnk), 0) + COALESCE(:sem_weight / (:k + sem.rnk), 0)"
        details = "kw.raw_score AS keyword_score, kw.rnk AS keyword_rank, sem.distance AS semantic_distance, sem.rnk AS semantic_rank"

    return f"""
    {ctes}
    SELECT c.id AS chunk_id, c.build_id, b.version_id, d.id AS document_id,
           d.kb_id, kb.name AS kb_name, d.title, c.text, c.locator, c.warnings, c.structure,
           {details},
           {score} AS score
    FROM chunks c
    JOIN index_builds b ON b.id = c.build_id
    JOIN documents d ON d.active_build_id = c.build_id
    JOIN knowledge_bases kb ON kb.id = d.kb_id
    {joins}
    {where}
    ORDER BY score DESC, {"kw.rnk" if bm25 and mode == "keyword" else "c.ordinal"}, c.id
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
    embedding_model: str | None = None,
    embedding_dim: int | None = None,
    optimized: bool = False,
    keyword_weight: float = 1.0,
    semantic_weight: float = 1.0,
    exact_terms: Sequence[str] = (),
    keyword_local_floor: float = 0.3,
    full_text: bool = False,
    bm25_candidates: Sequence[tuple[uuid.UUID, float]] | None = None,
) -> list[SearchHit]:
    if mode in ("semantic", "hybrid") and vector is None:
        raise ValueError("该模式需要查询向量")

    bm25 = bm25_candidates is not None
    statement = text(_build_sql(mode, optimized, full_text, bm25)).bindparams(bindparam("kb_ids", expanding=True))
    params = {
        "owner": actor.subject_id,
        "kb_ids": list(kb_ids),
        "q": query,
        "floor": similarity_floor,
        "recall": recall_limit,
        "top_k": top_k,
        "k": rrf_k,
        "max_distance": max_distance,
        "model": embedding_model,
        "dimension": embedding_dim,
        "kw_weight": keyword_weight,
        "sem_weight": semantic_weight,
        "terms": list(exact_terms),
        "local_floor": keyword_local_floor,
    }
    if vector is not None:
        params["vec"] = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
    if bm25_candidates is not None:
        unique = dict(bm25_candidates)
        params["bm25_ids"] = list(unique)
        params["bm25_scores"] = list(unique.values())

    rows = session.execute(statement, params).mappings().all()
    if mode in ("semantic", "hybrid"):
        assert_compatible(session, actor, kb_ids, embedding_model, embedding_dim)
    score_type = {
        "keyword": "bm25" if bm25 else "rrf",
        "hybrid": "bm25_rrf" if bm25 else "rrf",
        "semantic": "cosine_rank",
    }[mode]
    return [
        SearchHit(
            document_id=str(row["document_id"]),
            kb_id=str(row["kb_id"]),
            kb_name=row["kb_name"],
            version_id=str(row["version_id"]),
            build_id=str(row["build_id"]),
            chunk_id=str(row["chunk_id"]),
            title=row["title"],
            text=row["text"],
            locator=row["locator"],
            score=float(row["score"]),
            score_type=score_type,
            warnings=tuple(row["warnings"] or ()),
            structure=row["structure"] or {},
            diagnostics={key: row[key] for key in (
                "keyword_score", "keyword_rank", "semantic_distance", "semantic_rank"
            )},
        )
        for row in rows
    ]


def visible_builds(
    session: Session, actor: Actor, kb_ids: Sequence[uuid.UUID],
    *, lock_builds: Sequence[uuid.UUID] = (),
) -> dict[uuid.UUID, uuid.UUID]:
    """返回当前授权的构建与文档。冷同步持文档共享锁，避免在删除后重新写入索引。"""
    sql = f"""
        SELECT b.id AS build_id, d.id AS document_id
        FROM documents d
        JOIN index_builds b ON b.id = d.active_build_id
        JOIN knowledge_bases kb ON kb.id = d.kb_id
        WHERE {_FILTERS}
    """
    if lock_builds:
        sql += " AND b.id = ANY(CAST(:builds AS uuid[])) ORDER BY d.id FOR SHARE OF d"
    statement = text(sql).bindparams(bindparam("kb_ids", expanding=True))
    return {
        row.build_id: row.document_id
        for row in session.execute(statement, {
            "owner": actor.subject_id, "kb_ids": list(kb_ids), "builds": list(lock_builds),
        })
    }


def keyword_rows(
    session: Session, builds: Sequence[uuid.UUID],
) -> Iterator[tuple[uuid.UUID, uuid.UUID, str]]:
    """在持有授权构建的文档锁后读取关键词文本。"""
    rows = session.execute(
        select(Chunk.build_id, Chunk.id, Chunk.tsv).where(Chunk.build_id.in_(builds))
        .execution_options(yield_per=250)
    )
    try:
        for row in rows:
            yield row[0], row[1], row[2]
    finally:
        rows.close()


def related(
    session: Session, actor: Actor, kb_ids: Sequence[uuid.UUID],
    hits: Sequence[SearchHit], limit: int,
) -> list[SearchHit]:
    """在授权的当前构建内读取同结构邻接片段或同表表头。旧索引不猜测关联。"""
    anchor_ids = [
        uuid.UUID(hit.chunk_id) for hit in hits
        if hit.structure.get("strategy") in {"structure-v2", "pdf-layout-v1"}
    ]
    if not anchor_ids or limit <= 0:
        return []
    sql = f"""
        SELECT c.id AS chunk_id, c.build_id, b.version_id, d.id AS document_id,
               d.kb_id, kb.name AS kb_name, d.title, c.text, c.locator, c.warnings, c.structure,
               a.id AS anchor_id
        FROM chunks a
        JOIN index_builds b ON b.id = a.build_id
        JOIN documents d ON d.active_build_id = a.build_id
        JOIN knowledge_bases kb ON kb.id = d.kb_id
        CROSS JOIN LATERAL (
            SELECT candidate.* FROM chunks candidate
            WHERE candidate.build_id = a.build_id AND candidate.id != a.id
              AND candidate.structure->>'strategy' = a.structure->>'strategy'
              AND (
                (candidate.structure->>'unit_id' = a.structure->>'unit_id'
                 AND candidate.structure->>'kind' IN ('text', 'article', 'heading')
                 AND a.structure->>'kind' IN ('text', 'article', 'heading')
                 AND abs(candidate.ordinal - a.ordinal) = 1)
                OR
                (NULLIF(a.structure->>'table_id', '') IS NOT NULL
                 AND candidate.structure->>'table_id' = a.structure->>'table_id'
                 AND candidate.structure->>'is_header' = 'true')
              )
            ORDER BY (candidate.structure->>'is_header' = 'true') DESC,
                     abs(candidate.ordinal - a.ordinal), candidate.id
            LIMIT :neighbor_limit
        ) c
        WHERE {_FILTERS} AND a.id IN :anchor_ids
        ORDER BY (c.structure->>'is_header' = 'true') DESC, a.id, c.ordinal
    """
    statement = text(sql).bindparams(
        bindparam("anchor_ids", expanding=True), bindparam("kb_ids", expanding=True)
    )
    rows = session.execute(statement, {
        "owner": actor.subject_id, "kb_ids": list(kb_ids),
        "anchor_ids": anchor_ids, "neighbor_limit": limit,
    }).mappings().all()
    return [SearchHit(
        document_id=str(row["document_id"]), kb_id=str(row["kb_id"]), kb_name=row["kb_name"],
        version_id=str(row["version_id"]), build_id=str(row["build_id"]),
        chunk_id=str(row["chunk_id"]), title=row["title"], text=row["text"],
        locator=row["locator"], score=0.0, score_type="related",
        warnings=tuple(row["warnings"] or ()), structure=row["structure"] or {},
        diagnostics={"neighbor_of": str(row["anchor_id"])},
    ) for row in rows]


def assert_compatible(
    session: Session,
    actor: Actor,
    kb_ids: Sequence[uuid.UUID],
    model: str | None,
    dimension: int | None,
) -> None:
    incompatible = session.scalar(
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
        .limit(1)
    )
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
