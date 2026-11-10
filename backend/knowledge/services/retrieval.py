"""检索用例：参数校验 → 范围授权 → 双路召回 → 融合。"""

from __future__ import annotations

import math
import unicodedata
import uuid
from dataclasses import replace

from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.entities import Actor, SearchHit
from knowledge.domain.errors import KeywordIndexUnavailable, ModelUnavailable, ValidationFailed
from knowledge.domain.evidence import coverage_order, structured_neighbors
from knowledge.domain.ports import Embedder, KeywordIndex, Reranker
from knowledge.domain.querying import evidence_requirements, plan_query
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


def _bm25_candidates(
    session: Session, actor: Actor, kb_ids: list[uuid.UUID], index: KeywordIndex,
    query: str, limit: int,
) -> list[tuple[uuid.UUID, float]]:
    builds = chunk_repo.visible_builds(session, actor, kb_ids)
    missing = index.missing_builds(list(builds))
    for offset in range(0, len(missing), 50):
        batch = missing[offset:offset + 50]
        # 使用独立短事务，索引同步结束即释放文档锁，生成器不会持有这些锁。
        with Session(session.get_bind()) as sync_session:
            current = chunk_repo.visible_builds(sync_session, actor, kb_ids, lock_builds=batch)
            if current:
                index.index_builds(current, chunk_repo.keyword_rows(sync_session, list(current)))
            for build_id in set(batch) - current.keys():
                builds.pop(build_id, None)
    return index.search(query, list(builds), limit)


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
    reranker: Reranker | None = None,
    candidate_limit: int | None = None,
    keyword_index: KeywordIndex | None = None,
) -> list[SearchHit]:
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

    fts_profile = settings.retrieval_profile == "fts-v1"
    bm25_profile = settings.retrieval_profile == "bm25-v1"
    single_query = fts_profile or bm25_profile
    optimized = settings.retrieval_profile != "baseline-v1"
    output_limit = max(limit, min(candidate_limit or limit, settings.evidence_candidate_limit))
    if not optimized:
        output_limit = limit
    plan = plan_query(text, 1 if single_query else settings.query_variants_max)
    use_fts = fts_profile and mode != "semantic" and not plan.exact_terms and not any(
        unicodedata.name(char, "").startswith(("CJK UNIFIED IDEOGRAPH", "CJK COMPATIBILITY IDEOGRAPH"))
        for char in text
    )
    queries = tuple(variant for variant in plan.variants if len(variant) <= settings.query_max_chars) if optimized else (text,)
    requirements = (
        tuple(dict.fromkeys(part for part in evidence_requirements(text)
                            if len(part) <= settings.query_max_chars))
        if optimized and not single_query and mode == "hybrid" and settings.evidence_coverage else ()
    )
    if len(requirements) < 2:
        requirements = ()

    bm25_candidates = None
    if bm25_profile and mode != "semantic":
        if keyword_index is None:
            raise KeywordIndexUnavailable("关键词索引未配置")
        bm25_candidates = _bm25_candidates(
            session, actor, kb_ids, keyword_index, text, settings.recall_limit,
        )

    vectors: dict[str, list[float]] = {}
    if mode in ("semantic", "hybrid"):
        chunk_repo.assert_compatible(session, actor, kb_ids, embedder.model, embedder.dimension)
        unique = list(dict.fromkeys((*queries, *requirements)))
        encoded = embedder.embed(unique)
        if len(encoded) != len(unique):
            raise ModelUnavailable("查询向量数量不符")
        vectors = dict(zip(unique, encoded, strict=True))

    def recall(variant: str, count: int, exact_terms: tuple[str, ...]) -> list[SearchHit]:
        return chunk_repo.search(
            session, actor, kb_ids=kb_ids, query=variant, mode=mode, vector=vectors.get(variant),
            top_k=count,
            recall_limit=settings.recall_limit, rrf_k=settings.rrf_k,
            similarity_floor=settings.similarity_floor, max_distance=settings.semantic_max_distance,
            embedding_model=embedder.model, embedding_dim=embedder.dimension,
            optimized=optimized, exact_terms=exact_terms, full_text=use_fts,
            keyword_local_floor=settings.keyword_local_floor,
            keyword_weight=settings.keyword_weight if optimized else 1.0,
            semantic_weight=settings.semantic_weight if optimized else 1.0,
            bm25_candidates=bm25_candidates,
        )

    if bm25_profile:
        hits = recall(text, max(output_limit, settings.recall_limit), ())
        if mode == "hybrid":
            hits.sort(key=lambda hit: (-hit.score, hit.chunk_id))
        return hits[:output_limit]

    combined: dict[str, SearchHit] = {}
    original_has_hits = False
    for variant_index, variant in enumerate(queries):
        hits = recall(
            variant, max(output_limit, settings.recall_limit) if optimized else output_limit,
            plan.exact_terms if optimized else (),
        )
        if not optimized:
            return hits[:output_limit]
        if variant_index == 0:
            original_has_hits = bool(hits)
        for hit in hits:
            # 扩展查询取每一路最好的排名，避免重复改写靠累加次数抬高分数。
            previous = combined.get(hit.chunk_id)
            chosen = hit if previous is None or hit.score > previous.score else previous
            variants = set(previous.diagnostics.get("query_variants", [])) if previous else set()
            variants.add(variant_index)
            combined[hit.chunk_id] = replace(chosen, diagnostics={
                **chosen.diagnostics, "query_variants": sorted(variants), "query_intent": plan.intent,
            })
    ranked = sorted(combined.values(), key=lambda hit: (-hit.score, hit.chunk_id))
    requirement_hits: list[list[SearchHit]] = []
    if requirements and original_has_hits:
        term_sets = [set(plan_query(part).exact_terms) for part in requirements]
        shared_terms = set.intersection(*term_sets)
        # 共同编号仍留在查询文本中，避免仅凭共同编号抢占每项的前 3 个候选。
        requirement_hits = [
            recall(part, 3, tuple(sorted(terms - shared_terms)))
            for part, terms in zip(requirements, term_sets, strict=True)
        ]
    if optimized and not fts_profile and settings.reranker_url and ranked:
        if reranker is None:
            raise ModelUnavailable("重排序服务未装配")
        candidates = ranked[:max(output_limit, settings.evidence_candidate_limit)]
        original_ids = {hit.chunk_id for hit in candidates}
        all_candidates = {hit.chunk_id: hit for hit in candidates}
        for group in requirement_hits:
            for hit in group:
                all_candidates.setdefault(hit.chunk_id, hit)
        candidates = list(all_candidates.values())
        scores = reranker.rank(text, ["\n".join([hit.title, *hit.structure.get("section_path", []), hit.text]) for hit in candidates])
        if len(scores) != len(candidates) or not all(math.isfinite(score) for score in scores):
            raise ModelUnavailable("重排序分数无效")
        reranked = sorted([
            replace(hit, score=score, score_type="rerank", diagnostics={**hit.diagnostics, "fusion_score": hit.score})
            for hit, score in zip(candidates, scores)
        ], key=lambda hit: (-hit.score, hit.chunk_id))
        by_id = {hit.chunk_id: hit for hit in reranked}
        ranked = [hit for hit in reranked if hit.chunk_id in original_ids]
        requirement_hits = [
            sorted((by_id[hit.chunk_id] for hit in group), key=lambda hit: (-hit.score, hit.chunk_id))
            for group in requirement_hits
        ]
    if requirement_hits:
        ranked = coverage_order(ranked, requirement_hits)
    return ranked[:output_limit]


def select_evidence(
    session: Session, actor: Actor, settings: Settings,
    kb_ids: list[uuid.UUID], hits: list[SearchHit],
) -> list[SearchHit]:
    """控制重复、片段数和字符预算；关联证据保留独立引用。"""
    if settings.retrieval_profile == "baseline-v1":
        return hits[:settings.evidence_max_items]
    selected: list[SearchHit] = []
    used = 0
    neighbor_limit = min(settings.evidence_neighbor_limit, max(0, settings.evidence_max_items - 1))
    if settings.retrieval_profile == "bm25-v1":
        neighbor_limit = 0
    coverage_count = sum(bool(hit.diagnostics.get("coverage_priority")) for hit in hits)
    anchor_limit = min(settings.evidence_max_items, max(
        1, settings.evidence_max_items - neighbor_limit, coverage_count,
    ))
    for hit in hits:
        normalized = "".join(hit.text.split())
        if not hit.diagnostics.get("coverage_priority") and any(
            hit.document_id == item.document_id and (
                normalized in "".join(item.text.split()) or "".join(item.text.split()) in normalized
            ) for item in selected
        ):
            continue
        if used + len(hit.text) > settings.evidence_max_chars:
            continue
        selected.append(hit)
        used += len(hit.text)
        if len(selected) >= anchor_limit:
            break
    related = chunk_repo.related(session, actor, kb_ids, selected, neighbor_limit) if neighbor_limit else []
    if settings.retrieval_profile == "optimized-v2" and settings.structured_context:
        related = structured_neighbors(selected, related)[:neighbor_limit]
    # 邻接槽没有可用关联时继续补主召回，避免无结构的旧索引无故少给证据。
    for hit in [*related, *hits]:
        if len(selected) >= settings.evidence_max_items:
            break
        if any(hit.chunk_id == item.chunk_id for item in selected):
            continue
        if any(
            hit.document_id == item.document_id and "".join(hit.text.split()) in "".join(item.text.split())
            for item in selected
        ):
            continue
        if used + len(hit.text) > settings.evidence_max_chars:
            continue
        selected.append(hit)
        used += len(hit.text)
        if len(selected) >= settings.evidence_max_items:
            break
    return selected
