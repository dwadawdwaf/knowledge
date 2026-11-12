"""单次检索与问答证据选择，共用 RAGFlow 召回及排序。"""

from __future__ import annotations

import math
import uuid
from dataclasses import replace

from sqlalchemy.orm import Session

from knowledge.adapters.ragflow_text import query_terms
from knowledge.config import Settings
from knowledge.domain.entities import Actor, SearchHit
from knowledge.domain.errors import ModelUnavailable, ValidationFailed
from knowledge.domain.ports import Embedder
from knowledge.domain.ragflow_query import QueryTerms
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import knowledge_bases as kb_repo
from knowledge.repositories import ragflow_search

MODES = {"hybrid", "keyword", "semantic"}


def index_status(session: Session, actor: Actor, settings: Settings | None = None) -> dict:
    counts = ragflow_search.index_status(session, actor)
    if settings is not None and settings.search_engine == "opensearch":
        from knowledge.adapters.ragflow_opensearch import NativeIndex
        from knowledge.repositories.native_search import scope_counts
        libraries = kb_repo.list_owned(session, actor)
        if libraries:
            builds = scope_counts(session, actor, [kb.id for kb in libraries])
            missing = NativeIndex(settings).missing(list(builds))
            counts["missing_keyword_chunks"] = sum(builds[b] for b in missing)
    return {"status": "attention" if any(counts.values()) else "ready", **counts}


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
    candidate_limit: int | None = None,
    document_ids: list[uuid.UUID] | None = None,
    query_warnings: set[str] | None = None,
) -> list[SearchHit]:
    """查询级警告在没有候选时仍可由调用方读取。"""
    text = validate_query(query, settings)

    if mode not in MODES:
        raise ValidationFailed(f"mode 必须是 {sorted(MODES)} 之一")
    if not kb_ids:
        raise ValidationFailed("knowledge_base_ids 不能为空")
    if len(kb_ids) > settings.kb_scope_max:
        raise ValidationFailed(f"知识库范围最多 {settings.kb_scope_max} 个")

    kb_repo.get_active(session, actor, kb_ids)

    limit = settings.top_k_default if top_k is None else top_k
    if limit < 1 or limit > settings.top_k_max:
        raise ValidationFailed(f"top_k 必须在 1~{settings.top_k_max} 之间")

    scoped_documents = document_ids or []
    ragflow_search.validate_document_scope(session, actor, kb_ids, scoped_documents)
    if settings.search_engine == "opensearch":
        from knowledge.adapters.ragflow_native_query import native_query
        terms = QueryTerms([], {}, [])
        has_recall = mode != "semantic" and native_query(text) is not None
    else:
        terms = query_terms(text) if mode != "semantic" else QueryTerms([], {}, [])
        has_recall = terms.has_recall
    if not has_recall and mode == "keyword":
        return []
    effective_mode = "semantic" if mode == "hybrid" and not has_recall else mode
    vector = None
    if effective_mode != "keyword":
        chunk_repo.assert_compatible(
            session, actor, kb_ids, embedder.model, embedder.dimension, document_ids=scoped_documents,
        )
        encoded_query = embedder.embed([text])
        if (
            len(encoded_query) != 1 or len(encoded_query[0]) != embedder.dimension
            or not all(map(math.isfinite, encoded_query[0])) or not any(encoded_query[0])
        ):
            raise ModelUnavailable("查询向量无效")
        vector = encoded_query[0]
    output_limit = max(limit, min(candidate_limit or limit, settings.evidence_candidate_limit))
    if settings.search_engine == "opensearch":
        from knowledge.services import native_search
        hits = native_search.search(
            session, actor, settings, query=text, kb_ids=kb_ids, document_ids=scoped_documents,
            terms=terms, mode=effective_mode, vector=vector, limit=output_limit,
        )
    else:
        hits = ragflow_search.recall(
        session, actor, kb_ids=kb_ids, document_ids=scoped_documents, terms=terms,
        mode=effective_mode, vector=vector, model=embedder.model, dimension=embedder.dimension,
        output_limit=output_limit,
        )
    if effective_mode != mode:
        warning = "query_terms_empty"
        if query_warnings is not None:
            query_warnings.add(warning)
        hits = [replace(hit, warnings=(*hit.warnings, warning)) for hit in hits]
    return hits



def select_evidence(
    session: Session, actor: Actor, settings: Settings,
    kb_ids: list[uuid.UUID], hits: list[SearchHit],
) -> list[SearchHit]:
    """按直接命中顺序去重，并遵守片段条数与字符预算。"""
    selected: list[SearchHit] = []
    used = 0
    for hit in hits:
        if len(selected) >= settings.evidence_max_items:
            break
        if used + len(hit.text) > settings.evidence_max_chars:
            continue
        normalized = "".join(hit.text.split())
        if any(
            hit.chunk_id == item.chunk_id or hit.document_id == item.document_id and (
                normalized in "".join(item.text.split()) or "".join(item.text.split()) in normalized
            )
            for item in selected
        ):
            continue
        selected.append(hit)
        used += len(hit.text)
    return selected
