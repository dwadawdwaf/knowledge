"""检索用例：参数校验 → 范围授权 → 双路召回 → 融合。"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.entities import Actor, SearchHit
from knowledge.domain.errors import ValidationFailed
from knowledge.domain.ports import Embedder
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import knowledge_bases as kb_repo

MODES = {"hybrid", "keyword", "semantic"}


def validate_query(query: str, settings: Settings) -> str:
    text = (query or "").strip()
    if not text:
        raise ValidationFailed("query 不能为空")
    if len(text) > settings.query_max_chars:
        raise ValidationFailed(f"query 超过 {settings.query_max_chars} 字符上限")
    return text


def search(
    session: Session,
    actor: Actor,
    settings: Settings,
    embedder: Embedder,
    *,
    query: str,
    kb_ids: list[uuid.UUID],
    mode: str = "hybrid",
    top_k: int | None = None,
) -> list[SearchHit]:
    text = validate_query(query, settings)

    if mode not in MODES:
        raise ValidationFailed(f"mode 必须是 {sorted(MODES)} 之一")
    if not kb_ids:
        raise ValidationFailed("knowledge_base_ids 不能为空")
    if len(kb_ids) > settings.kb_scope_max:
        raise ValidationFailed(f"知识库范围最多 {settings.kb_scope_max} 个")

    kb_repo.get_active(session, actor, kb_ids)

    limit = top_k or settings.top_k_default
    if limit < 1 or limit > settings.top_k_max:
        raise ValidationFailed(f"top_k 必须在 1~{settings.top_k_max} 之间")

    vector = None
    if mode in ("semantic", "hybrid"):
        vector = embedder.embed([text])[0]

    return chunk_repo.search(
        session,
        actor,
        kb_ids=kb_ids,
        query=text,
        mode=mode,
        vector=vector,
        top_k=limit,
        recall_limit=settings.recall_limit,
        rrf_k=settings.rrf_k,
        similarity_floor=settings.similarity_floor,
        max_distance=settings.semantic_max_distance,
    )
