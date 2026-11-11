"""RAGFlow 候选召回、预选及相关度排序的 PostgreSQL 实现。"""

from __future__ import annotations

import json
import uuid
from collections.abc import Sequence
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import RowMapping
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, SearchHit
from knowledge.domain.errors import KeywordIndexUnavailable, ModelUnavailable, NotFound
from knowledge.domain.ragflow_query import (
    FIELD_WEIGHTS,
    LEXICAL_REVISION,
    QueryTerms,
    phrase_queries,
    position_key,
)

_PUBLISHED = """
    FROM documents d
    JOIN knowledge_bases kb ON kb.id = d.kb_id
    JOIN index_builds b ON b.id = d.active_build_id
"""
_SCOPE = _PUBLISHED + "JOIN chunks c ON c.build_id = b.id"
_FILTER = """
    kb.owner_id = :owner AND kb.id IN :kb_ids AND kb.status = 'active'
    AND d.status = 'active' AND b.state = 'ready'
    AND (NOT :limited OR d.id = ANY(CAST(:document_ids AS uuid[])))
"""
_FIELDS = """
    c.id AS chunk_id, c.text, c.locator, c.warnings, c.structure, c.ordinal,
    d.id AS document_id, d.title, kb.id AS kb_id, kb.name AS kb_name,
    b.id AS build_id, b.version_id
"""

_TERM_FIELDS = (*FIELD_WEIGHTS, *(f"{field}_positions" for field in FIELD_WEIGHTS), "ranking")
_TERM_SHAPE = 'strict $ ? (@.type() == "object" && ' + " && ".join(
    f'@.{field}.type() == "array"' for field in _TERM_FIELDS
) + ")"
_LEXICAL_ORDER = "lexical_score DESC, coverage DESC, ordinal, id"
_DENSE_ORDER = "cosine DESC, ordinal, id"


def outdated_terms_sql(table: str) -> str:
    """搜索与回填共用同一就绪条件，table 为代码内固定的表名或别名。"""
    return (
        f"({table}.lexical_revision < {LEXICAL_REVISION} OR "
        f"jsonb_path_exists({table}.fields, '{_TERM_SHAPE}', '{{}}', true) IS NOT TRUE)"
    )


def index_status(session: Session, actor: Actor) -> dict[str, int]:
    """只统计当前用户可访问的发布分块和未结束旧任务，不暴露全库计数。"""
    statement = text(f"""
        SELECT (
            SELECT count(*) {_SCOPE}
            LEFT JOIN chunk_search_terms terms ON terms.chunk_id = c.id
            WHERE kb.owner_id = :owner AND kb.status = 'active'
              AND d.status = 'active' AND b.state = 'ready'
              AND {outdated_terms_sql('terms')}
        ) AS missing_keyword_chunks, (
            SELECT count(*) FROM jobs j JOIN index_builds b ON b.id = j.build_id
            JOIN documents d ON d.id = j.resource_id
            JOIN knowledge_bases kb ON kb.id = d.kb_id
            WHERE kb.owner_id = :owner AND kb.status = 'active' AND d.status = 'active'
              AND j.kind IN ('ingest', 'reindex') AND j.state IN ('queued', 'running')
              AND b.chunk_config->>'strategy' IS DISTINCT FROM 'ragflow-v1'
        ) AS legacy_pending_jobs
    """)
    row = session.execute(statement, {"owner": actor.subject_id}).mappings().one()
    return {key: int(value) for key, value in row.items()}


# 按 Lucene slop=2 的位置偏移范围匹配。固定起点后，同一词的可用位置区间
# 单调递增，逐次取最早的可用位置即可，且不会把一次出现用于两个重复词。
_WHOLE_PHRASE_MATCH = """
    WITH RECURSIVE occurrences AS MATERIALIZED (
        SELECT document.positions, query.ordinals
        FROM (
            SELECT token, array_agg(CAST(ordinal AS integer) ORDER BY ordinal) AS ordinals
            FROM unnest(phrase.tokens) WITH ORDINALITY AS words(token, ordinal)
            GROUP BY token
        ) query
        JOIN unnest(source_vector.value) AS document ON document.lexeme = query.token
    ),
    starts AS (
        SELECT DISTINCT position - 1 - offset_by.amount AS origin, allowed.distance
        FROM occurrences CROSS JOIN LATERAL unnest(positions) AS found(position)
        CROSS JOIN generate_series(1, 2) AS allowed(distance)
        CROSS JOIN LATERAL generate_series(0, allowed.distance) AS offset_by(amount)
        WHERE ordinals[1] = 1
    ),
    placements AS (
        SELECT origin, distance, ordinals, positions, 0 AS step, 0 AS placed
        FROM starts CROSS JOIN occurrences
        UNION ALL
        SELECT previous.origin, previous.distance, previous.ordinals, previous.positions,
               previous.step + 1, choice.placed
        FROM placements previous
        CROSS JOIN LATERAL (
            SELECT CAST(min(position) AS integer) AS placed
            FROM unnest(previous.positions) AS found(position)
            WHERE position > previous.placed
              AND position BETWEEN previous.origin + previous.ordinals[previous.step + 1]
                               AND previous.origin + previous.ordinals[previous.step + 1] + previous.distance
        ) choice
        WHERE previous.step < cardinality(previous.ordinals) AND choice.placed IS NOT NULL
    )
    SELECT max(1.0 / (1 + complete.distance))
    FROM (
        SELECT distance FROM placements WHERE step = cardinality(ordinals)
        GROUP BY origin, distance HAVING count(*) = (SELECT count(*) FROM occurrences)
    ) complete
"""


def validate_document_scope(
    session: Session, actor: Actor, kb_ids: list[uuid.UUID], document_ids: list[uuid.UUID],
) -> None:
    if not document_ids:
        return
    statement = text("""
        SELECT count(*) FROM documents d JOIN knowledge_bases kb ON kb.id = d.kb_id
        WHERE kb.owner_id = :owner AND kb.id IN :kb_ids AND kb.status = 'active'
        AND d.status = 'active' AND d.id = ANY(CAST(:document_ids AS uuid[]))
    """).bindparams(bindparam("kb_ids", expanding=True))
    count = session.scalar(statement, {
        "owner": actor.subject_id, "kb_ids": kb_ids, "document_ids": document_ids,
    })
    if count != len(set(document_ids)):
        raise NotFound("文档不存在")


def _query_features(parameter: str) -> str:
    return f"""
        SELECT feature -> 0 ->> 0 AS anchor,
               ARRAY(SELECT value FROM jsonb_array_elements_text(feature -> 0)
                     WHERE value NOT LIKE 'pair:%' AND value NOT LIKE 'phrase:%') AS primary_terms,
               ARRAY(SELECT value FROM jsonb_array_elements_text(feature -> 0)
                     WHERE value LIKE 'pair:%' OR value LIKE 'phrase:%') AS primary_phrases,
               ARRAY(SELECT value FROM jsonb_array_elements_text(feature -> 1)
                     WHERE value NOT LIKE 'pair:%' AND value NOT LIKE 'phrase:%') AS aliases,
               ARRAY(SELECT value FROM jsonb_array_elements_text(feature -> 1)
                     WHERE value LIKE 'pair:%' OR value LIKE 'phrase:%') AS phrase_aliases,
               CAST(feature ->> 2 AS double precision) AS weight,
               CAST(feature ->> 3 AS double precision) AS alias_weight
        FROM jsonb_array_elements(CAST(:{parameter} AS jsonb)) AS q(feature)
    """


def _statement(mode: str, *, scoped: bool = False, trace: bool = False) -> str:
    distance = "c.embedding <=> CAST(:vector AS vector)"
    cosine = f"COALESCE(NULLIF(1 - ({distance}), CAST('NaN' AS double precision)), 0)"
    compatible = "b.embedding_model = :model AND b.embedding_dim = :dimension"
    vector_health = f"""
        vector_health AS MATERIALIZED (
            SELECT NOT EXISTS (
                SELECT 1 {_PUBLISHED} WHERE {_FILTER} AND NOT ({compatible})
            ) AS vector_ready
        )
    """
    vector_ready = "AND (SELECT vector_ready FROM vector_health)"
    if mode == "semantic":
        # 指定文档先用发布构建缩小范围，再做精确距离排序，避免共享 HNSW 候选被范围过滤耗尽。
        ordering = "score DESC, c.id" if scoped else distance
        nearest_limit = ":output_limit" if scoped else "1024"
        return f"""
            WITH {vector_health}, nearest AS MATERIALIZED (
                SELECT c.id, {cosine} AS score
                {_SCOPE}
                WHERE {_FILTER} AND {compatible}
                  {vector_ready}
                ORDER BY {ordering} LIMIT {nearest_limit}
            ), hits AS (
                SELECT {_FIELDS}, n.score, NULL AS coverage, NULL AS lexical_rank, NULL AS dense_rank,
                       n.score AS cosine, n.score AS preliminary
                {_SCOPE}
                JOIN nearest n ON n.id = c.id
                WHERE n.score >= :vector_floor
                ORDER BY n.score DESC, chunk_id LIMIT :output_limit
            )
            SELECT hits.*, vector_health.vector_ready
            FROM vector_health LEFT JOIN hits ON vector_health.vector_ready
            ORDER BY hits.score DESC, hits.chunk_id
        """

    field_columns = ", ".join(
        f"{name} jsonb" for field in FIELD_WEIGHTS for name in (field, f"{field}_positions")
    )
    field_rows = ", ".join(
        f"({weight / max(FIELD_WEIGHTS.values())}, unpacked.{field}, unpacked.{field}_positions)"
        for field, weight in FIELD_WEIGHTS.items()
    )
    # 授权及 GIN 粗筛先物化，防止相关评分子查询被规划到文档关联内反复执行。
    # 字段与短语结果用 OFFSET 0 保持一次求值；不提前截断，不改变召回及评分语义。
    lexical = f"""
        WITH {f'{vector_health},' if mode == 'hybrid' else ''} index_health AS MATERIALIZED (
            SELECT NOT EXISTS (
                SELECT 1
                {_SCOPE}
                LEFT JOIN chunk_search_terms idx ON idx.chunk_id = c.id
                WHERE {_FILTER}
                  AND {outdated_terms_sql('idx')}
            ) AS ready
        ),
        phrases AS MATERIALIZED (
            SELECT term, CAST(queries ->> 0 AS tsquery) AS exact, CAST(queries ->> 1 AS tsquery) AS near
            FROM jsonb_each(CAST(:proximity AS jsonb)) AS q(term, queries)
        ),
        query_groups AS MATERIALIZED (
            SELECT CAST(entry ->> 0 AS integer) AS clause_id,
                   ARRAY(SELECT jsonb_array_elements_text(entry -> 1)) AS variants,
                   CAST(entry ->> 2 AS tsquery) AS near
            FROM jsonb_array_elements(CAST(:groups AS jsonb)) AS q(entry)
        ),
        exact_phrases AS MATERIALIZED (
            SELECT term, CAST(query AS tsquery) AS exact
            FROM jsonb_each_text(CAST(:exact_phrases AS jsonb)) AS phrase(term, query)
        ),
        whole_aliases AS MATERIALIZED (
            SELECT weights.term, CAST(weights.weight AS double precision) AS weight,
                   exact_phrases.term IS NOT NULL AS positional
            FROM jsonb_each_text(CAST(:whole_aliases AS jsonb)) AS weights(term, weight)
            LEFT JOIN exact_phrases USING (term)
        ),
        whole_phrases AS MATERIALIZED (
            SELECT ARRAY(SELECT jsonb_array_elements_text(entry -> 0)) AS tokens,
                   CAST(entry ->> 1 AS tsquery) AS exact,
                   CAST(entry ->> 2 AS double precision) AS weight,
                   ARRAY(SELECT CAST(value AS integer) FROM jsonb_array_elements_text(entry -> 3)) AS clause_ids
            FROM jsonb_array_elements(CAST(:whole_phrases AS jsonb)) AS data(entry)
        ),
        recall_features AS MATERIALIZED ({_query_features("recall_features")}),
        scoped_terms AS MATERIALIZED (
            SELECT c.id, c.ordinal, {'c.embedding,' if mode == 'hybrid' else ''}
                   idx.terms, idx.fields
            {_SCOPE}
            JOIN chunk_search_terms idx ON idx.chunk_id = c.id
            WHERE {_FILTER}
              AND (SELECT ready FROM index_health)
              AND idx.terms && CAST(:terms AS text[])
              {f'AND {compatible} {vector_ready}' if mode == 'hybrid' else ''}
        ),
        eligible AS MATERIALIZED (
            SELECT c.id, c.ordinal, {f'{cosine} AS cosine,' if mode == 'hybrid' else ''}
                   overlap.matched / CAST(:clause_count AS double precision) AS coverage,
                   lexical_fields.score AS lexical_score
            FROM scoped_terms c
            CROSS JOIN LATERAL (SELECT c.terms, c.fields OFFSET 0) idx
            CROSS JOIN LATERAL (
                WITH streams AS MATERIALIZED (
                    SELECT CAST(source.stream AS tsvector) AS value
                    FROM jsonb_each(idx.fields) AS field(name, values)
                    CROSS JOIN LATERAL jsonb_array_elements_text(
                        CASE WHEN right(field.name, 10) = '_positions' THEN field.values
                             ELSE CAST('[]' AS jsonb) END
                    ) AS source(stream)
                ), matched_terms AS MATERIALIZED (
                    SELECT DISTINCT clause_id FROM query_groups
                    WHERE idx.terms && variants OR (near IS NOT NULL AND EXISTS (
                        SELECT 1 FROM streams WHERE streams.value @@ query_groups.near
                    ))
                )
                SELECT count(*) AS matched FROM (
                    SELECT clause_id FROM matched_terms
                    UNION ALL
                    SELECT clause.id FROM whole_phrases phrase
                    CROSS JOIN unnest(phrase.clause_ids) AS clause(id)
                    WHERE NOT EXISTS (SELECT 1 FROM matched_terms WHERE clause_id = clause.id)
                    AND EXISTS (
                        SELECT 1 FROM streams source_vector WHERE CASE
                            WHEN source_vector.value @@ phrase.exact THEN true
                            WHEN tsvector_to_array(source_vector.value) @> phrase.tokens THEN
                                ({_WHOLE_PHRASE_MATCH}) > 0
                            ELSE false END
                    )
                ) matched_clauses
            ) overlap
            CROSS JOIN LATERAL (
                SELECT COALESCE(max((COALESCE(field_score.score, 0) + phrase_score.score + whole_alias_score.score)
                    * CAST(source.weight AS double precision)), 0) AS score
                FROM LATERAL (
                    SELECT * FROM jsonb_to_record(idx.fields) AS fields({field_columns})
                    OFFSET 0
                ) unpacked
                CROSS JOIN LATERAL (
                    SELECT * FROM (VALUES {field_rows}) AS values_by_field(weight, keys, streams)
                    OFFSET 0
                ) source
                CROSS JOIN LATERAL (
                    SELECT ARRAY(SELECT phrase.term FROM exact_phrases phrase WHERE EXISTS (
                        SELECT 1 FROM jsonb_array_elements_text(source.streams) AS positions(stream)
                        WHERE CAST(stream AS tsvector) @@ phrase.exact
                    )) AS matched
                    OFFSET 0
                ) phrase_hits
                CROSS JOIN LATERAL (
                    SELECT sum(f.weight * CASE
                        WHEN source.keys ?| f.primary_terms OR phrase_hits.matched && f.primary_phrases THEN 1
                        ELSE GREATEST(CASE WHEN source.keys ?| f.aliases OR phrase_hits.matched && f.phrase_aliases
                                            THEN f.alias_weight ELSE 0 END,
                            CASE WHEN phrases.near IS NOT NULL THEN (
                                SELECT max(CASE
                                    WHEN CAST(stream AS tsvector) @@ phrases.exact THEN 1
                                    WHEN CAST(stream AS tsvector) @@ phrases.near THEN 0.5
                                    ELSE 0 END)
                                FROM jsonb_array_elements_text(source.streams) AS positions(stream)
                            ) END, 0) END) AS score
                    FROM recall_features f
                    LEFT JOIN phrases ON phrases.term = f.anchor
                ) field_score
                CROSS JOIN LATERAL (
                    SELECT COALESCE(sum(alias.weight), 0) AS score
                    FROM whole_aliases alias
                    WHERE CASE WHEN alias.positional THEN alias.term = ANY(phrase_hits.matched)
                               ELSE source.keys ? alias.term END
                ) whole_alias_score
                CROSS JOIN LATERAL (
                    SELECT COALESCE(sum(phrase.weight * hit.score), 0) AS score
                    FROM whole_phrases phrase
                    CROSS JOIN LATERAL (
                        SELECT max(CASE
                            WHEN source_vector.value @@ phrase.exact THEN 1
                            WHEN tsvector_to_array(source_vector.value) @> phrase.tokens THEN
                                COALESCE(({_WHOLE_PHRASE_MATCH}), 0)
                            ELSE 0 END) AS score
                        FROM jsonb_array_elements_text(source.streams) AS positions(stream)
                        CROSS JOIN LATERAL (SELECT CAST(positions.stream AS tsvector) AS value) source_vector
                    ) hit
                ) phrase_score
            ) lexical_fields
            WHERE overlap.matched >= :minimum
        ),
        lexical AS (
            SELECT id, row_number() OVER (ORDER BY {_LEXICAL_ORDER}) AS lexical_rank
            FROM (
                SELECT id, ordinal, lexical_score, coverage FROM eligible
                ORDER BY {_LEXICAL_ORDER} LIMIT 1024
            ) top
        )
    """
    if mode == "keyword":
        return lexical + f"""
            , hits AS (
                SELECT {_FIELDS}, e.lexical_score AS score, e.coverage, l.lexical_rank,
                       NULL AS dense_rank, NULL AS cosine, e.lexical_score AS preliminary
                {_SCOPE}
                JOIN lexical l ON l.id = c.id JOIN eligible e ON e.id = c.id
                ORDER BY l.lexical_rank LIMIT :output_limit
            )
            SELECT hits.*, index_health.ready AS index_ready
            FROM index_health LEFT JOIN hits ON index_health.ready
            ORDER BY hits.lexical_rank
        """
    hybrid = lexical + f"""
        , dense AS (
            SELECT id, row_number() OVER (ORDER BY {_DENSE_ORDER}) AS dense_rank
            FROM (
                SELECT id, ordinal, cosine FROM eligible WHERE cosine >= :vector_floor
                ORDER BY {_DENSE_ORDER} LIMIT 1024
            ) top
        ),
        shortlist AS (
            SELECT s.*, l.lexical_rank, v.dense_rank,
                   0.05 * CASE WHEN l.id IS NULL THEN 0 ELSE s.lexical_score END
                     + 0.95 * CASE WHEN v.id IS NULL THEN 0 ELSE (1 + s.cosine) / 2 END AS preliminary
            FROM lexical l FULL JOIN dense v USING (id)
            JOIN eligible s ON s.id = COALESCE(l.id, v.id)
            ORDER BY preliminary DESC, s.lexical_score DESC, s.coverage DESC, s.id
            LIMIT 64
        )
    """
    if trace:
        return hybrid + """
            SELECT
                EXISTS (SELECT 1 FROM eligible WHERE id = ANY(CAST(:gold AS uuid[]))) AS keyword_gate,
                EXISTS (SELECT 1 FROM lexical WHERE id = ANY(CAST(:gold AS uuid[]))) AS lexical_top,
                EXISTS (SELECT 1 FROM dense WHERE id = ANY(CAST(:gold AS uuid[]))) AS dense_top,
                EXISTS (SELECT 1 FROM shortlist WHERE id = ANY(CAST(:gold AS uuid[]))) AS shortlist
        """
    return hybrid + f"""
        , hits AS (
            SELECT {_FIELDS}, p.coverage, p.lexical_rank, p.dense_rank, p.cosine, p.preliminary,
                   0.7 * ranked.coverage + 0.3 * COALESCE(p.cosine, 0) AS score
            {_SCOPE}
            JOIN shortlist p ON p.id = c.id
            JOIN chunk_search_terms idx ON idx.chunk_id = p.id
            CROSS JOIN LATERAL (
                SELECT COALESCE(sum(CAST(f.weight AS double precision)), 0) AS coverage
                FROM jsonb_each_text(CAST(:features AS jsonb)) AS f(term, weight)
                WHERE (idx.fields -> 'ranking') ? f.term
            ) ranked
        )
        SELECT hits.*, index_health.ready AS index_ready, vector_health.vector_ready
        FROM index_health CROSS JOIN vector_health
        LEFT JOIN hits ON index_health.ready AND vector_health.vector_ready
        ORDER BY hits.score DESC, hits.chunk_id
    """


def recall(
    session: Session, actor: Actor, *, kb_ids: list[uuid.UUID], document_ids: list[uuid.UUID],
    terms: QueryTerms, mode: str, vector: Sequence[float] | None,
    model: str, dimension: int, output_limit: int,
    gold: Sequence[uuid.UUID] = (), trace: dict[str, bool] | None = None,
) -> list[SearchHit]:
    by_term = {group[0]: group for group in terms.groups}
    clauses = terms.clauses or tuple((term,) for term in by_term)
    clause_count = len(clauses)
    proximity = {term: phrase_queries(words) for term, words in terms.proximity.items()}
    whole_aliases: dict[str, float] = {}
    for clause_weights in terms.clause_aliases.values():
        for alias, weight in clause_weights.items():
            whole_aliases[alias] = whole_aliases.get(alias, 0.0) + weight
    keys = {term for group in terms.groups for term in group} | whole_aliases.keys()
    positional = keys | {
        word for primary, alternatives, _, _ in terms.recall_features for word in (*primary, *alternatives)
    }
    phrase_words = {
        word: word.split(":", 1)[1].split() for word in positional if word.startswith(("pair:", "phrase:"))
    }
    exact_phrases = {}
    for word, tokens in phrase_words.items():
        encoded = list(map(position_key, tokens))
        if all(len(token) < 2047 for token in encoded):
            exact_phrases[word] = " <1> ".join(encoded)
    groups: list[tuple[int, list[str], str | None]] = []
    for index, clause in enumerate(clauses):
        clause_alternatives = terms.clause_aliases.get(index, {})
        matches = exact_phrases.keys() & clause_alternatives.keys()
        for term in clause:
            alternatives = by_term[term]
            variants = [word for word in alternatives if word not in phrase_words]
            near = proximity[term][1] if term in proximity else None
            if variants or near:
                groups.append((index, variants, near))
            matches.update(exact_phrases.keys() & set(alternatives))
        direct_aliases = [word for word in clause_alternatives if word not in phrase_words]
        if direct_aliases:
            groups.append((index, direct_aliases, None))
        groups.extend((index, [], exact_phrases[word]) for word in sorted(matches))
    whole_phrases = []
    for words, weight in terms.whole_phrases.items():
        tokens = list(map(position_key, words))
        indexes = [index for index, phrase in terms.clause_phrases.items() if phrase == words]
        whole_phrases.append((tokens, " <1> ".join(tokens), weight, indexes))
        if indexes:
            keys.update(words)
    keys.update(word for key in keys.intersection(phrase_words) for word in phrase_words[key])
    keys.update(word for phrase in terms.proximity.values() for word in phrase)
    params: dict[str, Any] = {  # SQL 参数有数组、数值及布尔类型。
        "owner": actor.subject_id, "kb_ids": kb_ids, "document_ids": document_ids,
        "limited": bool(document_ids), "model": model, "dimension": dimension,
        "groups": json.dumps(groups, ensure_ascii=False),
        "terms": sorted(keys), "proximity": json.dumps(proximity, ensure_ascii=False),
        "exact_phrases": json.dumps(exact_phrases, ensure_ascii=False),
        "whole_aliases": json.dumps(whole_aliases, ensure_ascii=False),
        "whole_phrases": json.dumps(whole_phrases),
        "clause_count": clause_count,
        "features": json.dumps(terms.features, ensure_ascii=False),
        "recall_features": json.dumps(terms.recall_features, ensure_ascii=False),
        "field_weights": json.dumps({
            field: weight / max(FIELD_WEIGHTS.values()) for field, weight in FIELD_WEIGHTS.items()
        }),
        "vector_floor": 0.20, "output_limit": output_limit,
        "minimum": max(1, clause_count * 3 // 10) if terms.use_minimum_match else 1,
    }
    if vector is not None:
        params["vector"] = "[" + ",".join(map(str, vector)) + "]"

    statement = text(_statement(mode, scoped=bool(document_ids))).bindparams(bindparam("kb_ids", expanding=True))

    def matching_rows() -> Sequence[RowMapping]:
        rows = session.execute(statement, params).mappings().all()
        # 生成向量期间可能有新构建发布，按实际召回的快照复核。
        if mode != "keyword" and not rows[0]["vector_ready"]:
            raise ModelUnavailable("索引与查询向量配置不兼容，请恢复原配置或重建索引")
        if mode != "semantic" and not rows[0]["index_ready"]:
            raise KeywordIndexUnavailable("所选资料的关键词索引尚未回填或更新")
        return [row for row in rows if row["chunk_id"] is not None]

    rows = matching_rows()
    retried = not rows and mode == "hybrid"
    if retried:
        params.update(
            minimum=max(1, clause_count // 10) if terms.use_minimum_match else 1,
            vector_floor=0.17,
        )
        rows = matching_rows()

    if trace is not None and mode == "hybrid":
        diagnostic = text(_statement(mode, trace=True)).bindparams(bindparam("kb_ids", expanding=True))
        trace.update(session.execute(diagnostic, {**params, "gold": list(gold)}).mappings().one())
        trace["recall_retry"] = retried

    score_type = {"keyword": "term_overlap", "semantic": "cosine", "hybrid": "ragflow_hybrid"}[mode]
    return [
        SearchHit(
            document_id=str(row["document_id"]), kb_id=str(row["kb_id"]), kb_name=row["kb_name"],
            version_id=str(row["version_id"]), build_id=str(row["build_id"]),
            chunk_id=str(row["chunk_id"]), title=row["title"], text=row["text"],
            locator=row["locator"], score=float(row["score"]), score_type=score_type,
            warnings=tuple(row["warnings"] or ()), structure=row["structure"] or {},
            diagnostics={**{key: row[key] for key in (
                "coverage", "lexical_rank", "dense_rank", "cosine", "preliminary",
            )}, "recall_retry": retried},
        )
        for row in rows[:output_limit] if mode != "hybrid" or row["score"] >= 0.20
    ]

