"""RAGFlow 式原生召回、有限候选重排与 PostgreSQL 回源。"""
from __future__ import annotations

import math
import uuid
from collections.abc import Sequence
from dataclasses import replace

from sqlalchemy.orm import Session

from knowledge.adapters.ragflow_native_query import native_features
from knowledge.adapters.ragflow_opensearch import NativeIndex
from knowledge.config import Settings
from knowledge.domain.entities import Actor, SearchHit
from knowledge.domain.errors import KeywordIndexUnavailable, ModelUnavailable
from knowledge.domain.ragflow_query import QueryTerms
from knowledge.repositories import native_search as repository


def synchronize(session: Session, actor: Actor, settings: Settings, builds: Sequence[uuid.UUID]) -> int:
    index = NativeIndex(settings)
    missing = index.missing(builds)
    if missing:
        rows = repository.export(session, actor, missing)
        if set(missing) != {row["build_id"] for row in rows}:
            raise KeywordIndexUnavailable("索引同步范围发生变化，请重新尝试")
        index.index(missing, rows)
    return len(missing)


def search(session: Session, actor: Actor, settings: Settings, *, query: str,
           kb_ids: list[uuid.UUID], document_ids: list[uuid.UUID], terms: QueryTerms,
           mode: str, vector: list[float] | None, limit: int) -> list[SearchHit]:
    builds = repository.scope(session, actor, kb_ids, document_ids)
    if not builds:
        return []
    index = NativeIndex(settings)
    if index.missing(builds):
        raise KeywordIndexUnavailable("搜索索引尚未同步，请执行原生索引回填")
    # 上游按页面大小取整重排窗口；Top20 的窗口是80，不是固定64。
    window = max(30, math.ceil(64 / limit) * limit) if mode == "hybrid" else limit
    ranking: dict[uuid.UUID, list[str]] = {}
    candidates = index.search(query, builds, mode, vector, window, ranking=ranking)
    retried = not candidates and mode == "hybrid"
    if retried:
        candidates = index.search(query, builds, mode, vector, window, minimum=.1, floor=.17, ranking=ranking)
    rows = repository.hydrate(session, actor, kb_ids, document_ids, [key for key, _ in candidates], with_vectors=vector is not None)
    scores = dict(candidates)
    hits = []
    features = native_features(query) if mode == "hybrid" else {}
    denominator = sum(features.values()) + 1e-9
    for row in rows:
        if mode != "keyword" and (row["embedding_model"] != settings.embedding_model or row["embedding_dim"] != settings.embedding_dim):
            raise ModelUnavailable("索引与查询向量配置不兼容")
        cosine = 0.0
        if vector is not None:
            norm = math.sqrt(sum(x*x for x in vector) * sum(x*x for x in row["embedding"]))
            if norm:
                cosine = sum(a*b for a,b in zip(vector,row["embedding"],strict=True)) / norm
        present = set(ranking.get(row["chunk_id"], []))
        coverage = (sum(w for term,w in features.items() if term in present) + 1e-9) / denominator
        score = scores[row["chunk_id"]] if mode == "keyword" else cosine if mode == "semantic" else .7*coverage+.3*cosine
        hits.append(SearchHit(
            document_id=str(row["document_id"]), kb_id=str(row["kb_id"]), kb_name=row["kb_name"],
            version_id=str(row["version_id"]), build_id=str(row["build_id"]), chunk_id=str(row["chunk_id"]),
            title=row["title"], text=row["text"], locator=row["locator"], structure=row["structure"] or {},
            warnings=tuple(row["warnings"] or []), score=score,
            score_type={"keyword":"ragflow_lexical","semantic":"cosine","hybrid":"ragflow_hybrid"}[mode],
            diagnostics={"coverage":coverage,"cosine":cosine,"preliminary":scores[row["chunk_id"]],"recall_retry":retried},
        ))
    if mode == "hybrid" and sum(h.diagnostics["cosine"] for h in hits) == 0:
        # 上游 hybrid_similarity 的零向量相似度分支直接采用词项覆盖分。
        hits = [replace(h, score=h.diagnostics["coverage"]) for h in hits]
    visible = [h for h in hits if mode == "keyword" or h.score >= .2]
    return sorted(visible,key=lambda h:(-h.score,h.chunk_id))[:limit]
