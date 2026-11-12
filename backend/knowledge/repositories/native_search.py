"""原生搜索索引的授权范围、派生数据导出与候选回源。"""
from __future__ import annotations

import uuid
from collections.abc import Sequence

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor
from knowledge.repositories.ragflow_search import _FIELDS, _FILTER, _SCOPE


def scope(session: Session, actor: Actor, kb_ids: Sequence[uuid.UUID],
          document_ids: Sequence[uuid.UUID] = ()) -> list[uuid.UUID]:
    return list(scope_counts(session, actor, kb_ids, document_ids))


def scope_counts(session: Session, actor: Actor, kb_ids: Sequence[uuid.UUID],
                 document_ids: Sequence[uuid.UUID] = ()) -> dict[uuid.UUID, int]:
    stmt = text(f"SELECT b.id, count(*) {_SCOPE} WHERE {_FILTER} GROUP BY b.id").bindparams(bindparam("kb_ids", expanding=True))
    return {row[0]: row[1] for row in session.execute(stmt, {
        "owner": actor.subject_id, "kb_ids": list(kb_ids), "limited": bool(document_ids),
        "document_ids": list(document_ids),
    })}


def export(session: Session, actor: Actor, builds: Sequence[uuid.UUID]) -> list[dict]:
    if not builds:
        return []
    # 可导出未发布构建，但必须属于该用户。发布资格仍由 Worker 租约单独校验。
    stmt = text("""
        SELECT c.id, c.build_id, c.text, c.structure, c.embedding, d.title
        FROM chunks c JOIN index_builds b ON b.id=c.build_id
        JOIN document_versions v ON v.id=b.version_id
        JOIN documents d ON d.id=v.document_id JOIN knowledge_bases kb ON kb.id=d.kb_id
        WHERE kb.owner_id=:owner AND kb.status='active' AND d.status='active'
          AND b.id IN :builds ORDER BY c.build_id,c.ordinal
    """).bindparams(bindparam("builds", expanding=True))
    # raw SQL 未经 pgvector SQLAlchemy 类型处理，显式解码向量。
    rows = [dict(r) for r in session.execute(stmt, {"owner": actor.subject_id, "builds": list(builds)}).mappings()]
    import json
    for row in rows:
        if isinstance(row["embedding"], str):
            row["embedding"] = json.loads(row["embedding"])
    return rows


def hydrate(session: Session, actor: Actor, kb_ids: Sequence[uuid.UUID],
            document_ids: Sequence[uuid.UUID], ids: Sequence[uuid.UUID], *, with_vectors: bool = True) -> list[dict]:
    if not ids:
        return []
    stmt = text(f"""
        SELECT {_FIELDS}, {'c.embedding' if with_vectors else 'NULL AS embedding'}, b.embedding_model, b.embedding_dim
        {_SCOPE}
        WHERE {_FILTER} AND c.id IN :ids
    """).bindparams(bindparam("kb_ids", expanding=True), bindparam("ids", expanding=True))
    rows = [dict(r) for r in session.execute(stmt, {
        "owner": actor.subject_id, "kb_ids": list(kb_ids), "limited": bool(document_ids),
        "document_ids": list(document_ids), "ids": list(ids),
    }).mappings()]
    import json
    for row in rows:
        if isinstance(row["embedding"], str):
            row["embedding"] = json.loads(row["embedding"])
    return rows
