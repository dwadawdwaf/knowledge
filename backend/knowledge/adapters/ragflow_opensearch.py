"""RAGFlow 原生 Lucene 多字段召回及过滤 kNN，HTTP 适配到 OpenSearch。"""
from __future__ import annotations

import itertools
import json
import math
import re
import urllib.error
import urllib.request
from collections.abc import Iterable, Sequence
from typing import Any
from uuid import UUID

from knowledge.adapters import outbound
from knowledge.adapters.ragflow_native_query import native_query
from knowledge.adapters.ragflow_tokenizer import tokenizer
from knowledge.config import Settings
from knowledge.domain.errors import KeywordIndexUnavailable

REVISION = 2
SCRIPTED_SIM = "double idf = Math.log(1+(field.docCount-term.docFreq+0.5)/(term.docFreq + 0.5))/Math.log(1+((field.docCount-0.5)/1.5)); return query.boost * idf * Math.min(doc.freq, 1);"
TEXT_FIELDS = ("title_tks", "title_sm_tks", "important_tks", "question_tks", "content_ltks", "content_sm_ltks")


def token_fields(body: str, title: str, structure: dict) -> dict:
    def tokens(value: str, fine: bool = False) -> str:
        primary = tokenizer().tokenize(value)
        return tokenizer().fine_grained_tokenize(primary) if fine else primary
    headings = " ".join(structure.get("section_path", []))
    header = structure.get("table_header", "")
    content = " ".join([body, *structure.get("table_fields", [])])
    # JSON 索引字段包含字符串和词项数组。
    result: dict[str, Any] = {
        "title_tks": tokens(title + " " + headings),
        "title_sm_tks": tokens(title + " " + headings, True),
        "important_kwd": tokens(header).split(),
        "important_tks": tokens(header, True), "question_tks": "",
        "content_ltks": tokens(content), "content_sm_ltks": tokens(content, True),
    }
    ordered = [*dict.fromkeys(result["content_ltks"].split()),
               *(result["title_tks"].split() * 2), *(result["important_kwd"] * 5)]
    result["rerank_terms"] = sorted(set(ordered) | {a+b for a,b in itertools.pairwise(ordered)})
    return result


class NativeIndex:
    def __init__(self, settings: Settings) -> None:
        if not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,150}", settings.opensearch_index):
            raise ValueError("OpenSearch 索引名不合法")
        self.url = settings.opensearch_url.rstrip("/") + "/" + settings.opensearch_index
        self.timeout = settings.opensearch_timeout_seconds
        self.dimension = settings.embedding_dim

    def request(self, method: str, path: str, body: object = None, missing_ok: bool = False) -> dict:
        # Any 只用于未经验证的远端 JSON；调用入口逐项校验响应。
        data = body if isinstance(body, bytes) else json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.url + path, data=data, method=method, headers={
            "Content-Type": "application/x-ndjson" if isinstance(body, bytes) else "application/json",
        })
        try:
            with outbound.open_request(req, timeout=self.timeout, follow_redirects=False) as res:
                value: Any = json.load(res)
        except urllib.error.HTTPError as exc:
            if missing_ok and exc.code == 404:
                exc.close()
                return {}
            exc.close()
            raise KeywordIndexUnavailable("搜索索引请求失败，请检查 OpenSearch 服务与索引") from None
        except (OSError, ValueError):
            raise KeywordIndexUnavailable("OpenSearch 服务不可用") from None
        if not isinstance(value, dict):
            raise KeywordIndexUnavailable("搜索索引响应无效")
        return value

    def create(self) -> None:
        existing = self.request("GET", "", missing_ok=True)
        if existing:
            if any(v.get("mappings", {}).get("_meta", {}).get("ragflow_revision") != REVISION for v in existing.values()):
                raise KeywordIndexUnavailable("搜索索引配置版本不兼容，请使用新的索引名回填")
            return
        self.request("PUT", "", {
            "settings": {"index.knn": True, "number_of_shards": 2, "number_of_replicas": 0,
                         "similarity": {"scripted_sim": {"type": "scripted", "script": {"source": SCRIPTED_SIM}}}},
            "mappings": {"dynamic": "strict", "_meta": {"ragflow_revision": REVISION}, "properties": {
                **{name: {"type": "text", "analyzer": "whitespace",
                          **({"similarity": "scripted_sim"} if name.endswith("_tks") else {})} for name in TEXT_FIELDS},
                **{name: {"type": "keyword"} for name in ("build_id", "key")},
                "important_kwd": {"type": "keyword", "similarity": "boolean"},
                "rerank_terms": {"type": "keyword", "index": False},
                "revision": {"type": "integer"},
                "embedding": {"type": "knn_vector", "dimension": self.dimension,
                              "method": {"name": "hnsw", "engine": "lucene", "space_type": "cosinesimil"}},
            }},
        })

    def missing(self, builds: Sequence[UUID]) -> list[UUID]:
        result: list[UUID] = []
        for offset in range(0, len(builds), 500):
            batch = builds[offset:offset + 500]
            markers = ["build-" + str(b) for b in batch]
            res = self.request("POST", "/_mget", {"ids": markers}, missing_ok=True)
            if not res:
                result.extend(batch)
                continue
            try:
                docs = res["docs"]
                assert {d["_id"] for d in docs} == set(markers)
                found = {d["_id"] for d in docs if d.get("found") is True and d.get("_source", {}).get("revision") == REVISION}
                result.extend(b for b in batch if "build-" + str(b) not in found)
            except (KeyError, TypeError, AssertionError):
                raise KeywordIndexUnavailable("搜索索引完成标记无效") from None
        return result

    def index(self, builds: Sequence[UUID], rows: Iterable[dict]) -> None:
        self.create()
        lines: list[str] = []
        def flush() -> None:
            if lines:
                res = self.request("POST", "/_bulk", ("\n".join(lines) + "\n").encode())
                if res.get("errors") is not False:
                    raise KeywordIndexUnavailable("搜索索引写入失败")
                lines.clear()
        for row in rows:
            key = str(row["id"])
            vector = list(map(float, row["embedding"]))
            if len(vector) != self.dimension or not any(vector) or not all(map(math.isfinite, vector)):
                raise KeywordIndexUnavailable("分块向量无效，无法建立原生向量索引")
            lines.extend([json.dumps({"index": {"_id": key}}), json.dumps({
                "key": key, "build_id": str(row["build_id"]), "embedding": vector,
                **token_fields(row["text"], row["title"], row["structure"] or {}),
            }, ensure_ascii=False)])
            if len(lines) >= 200:
                flush()
        flush()
        if self.request("POST", "/_refresh").get("_shards", {}).get("failed") != 0:
            raise KeywordIndexUnavailable("搜索索引刷新失败")
        for build in builds:
            lines.extend([json.dumps({"index": {"_id": "build-" + str(build)}}), json.dumps({"revision": REVISION})])
        flush()
        if self.request("POST", "/_refresh").get("_shards", {}).get("failed") != 0:
            raise KeywordIndexUnavailable("搜索索引完成标记刷新失败")

    def delete(self, builds: Sequence[UUID]) -> None:
        if not builds:
            return
        res = self.request("POST", "/_delete_by_query?refresh=true", {"query": {"bool": {"should": [
            {"terms": {"build_id": list(map(str, builds))}},
            {"ids": {"values": ["build-" + str(b) for b in builds]}},
        ], "minimum_should_match": 1}}}, missing_ok=True)
        if res and (res.get("failures") or res.get("timed_out") or res.get("version_conflicts")):
            raise KeywordIndexUnavailable("搜索索引清理失败")

    def search(self, query: str, builds: Sequence[UUID], mode: str, vector: list[float] | None,
               limit: int, minimum: float = .3, floor: float = .2,
               ranking: dict[UUID, list[str]] | None = None) -> list[tuple[UUID, float]]:
        scope = {"terms": {"build_id": sorted(map(str, builds))}}
        if not builds:
            return []
        lexical = native_query(query, minimum) if mode != "semantic" else None
        if mode == "keyword" and lexical is None:
            return []
        text_query = {"bool": {"filter": [scope], "must": [lexical] if lexical else []}}
        expression: dict[str, Any]
        if mode == "keyword":
            expression = text_query
        else:
            dense = {"function_score": {
                "query": {"knn": {"embedding": {"vector": vector, "k": 1024, "filter": text_query}}},
                "min_score": (1 + floor) / 2,
            }}
            expression = dense if mode == "semantic" else {"bool": {
                "should": [{"bool": {"must": [text_query], "boost": .05}},
                           {"bool": {"must": [dense], "boost": 1.0}}], "minimum_should_match": 1,
            }}
        res = self.request("POST", "/_search?allow_partial_search_results=false", {
            "query": expression, "size": limit, "_source": ["rerank_terms"] if mode == "hybrid" else False, "track_total_hits": False,
            "sort": [{"_score": "desc"}, {"key": "asc"}],
        })
        try:
            if res.get("timed_out") is not False or res.get("_shards", {}).get("failed") != 0:
                raise ValueError
            hits = [(UUID(h["_id"]), float(h["_score"])) for h in res["hits"]["hits"]]
            if len(hits) > limit or any(not math.isfinite(s) for _, s in hits):
                raise ValueError
            if ranking is not None and mode == "hybrid":
                for hit in res["hits"]["hits"]:
                    terms = hit.get("_source", {}).get("rerank_terms")
                    if not isinstance(terms, list) or not all(isinstance(t, str) for t in terms):
                        raise ValueError
                    ranking[UUID(hit["_id"])] = terms
            return hits
        except (KeyError, TypeError, ValueError):
            raise KeywordIndexUnavailable("搜索索引返回了无效候选") from None
