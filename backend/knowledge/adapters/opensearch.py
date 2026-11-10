"""本机 OpenSearch BM25。只返回候选 ID 与分数，不返回原文。"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Iterable, Mapping, Sequence
from itertools import islice
from typing import Any
from uuid import UUID

from knowledge.adapters import outbound
from knowledge.config import Settings
from knowledge.domain.errors import KeywordIndexUnavailable

_INDEX = {
    "settings": {
        "number_of_shards": 1, "number_of_replicas": 0,
        "similarity": {"ks_bm25": {"type": "BM25", "k1": 1.2, "b": 0.75}},
    },
    "mappings": {
        "dynamic": "strict",
        "_source": {"enabled": False},
        "properties": {
            "key": {"type": "keyword"},
            "build_id": {"type": "keyword"},
            "document_id": {"type": "keyword"},
            "content": {"type": "text", "analyzer": "english", "similarity": "ks_bm25"},
        },
    },
}


class OpenSearchKeywordIndex:
    def __init__(self, url: str, index: str, timeout: float) -> None:
        self.url = f"{url.rstrip('/')}/{index}"
        self.timeout = timeout

    def _request(
        self, method: str, path: str, body: object = None, *, missing_ok: bool = False,
        create: bool = False,
    ) -> dict[str, Any] | None:
        # 这里的 Any 只用于未验证的 OpenSearch JSON，调用方校验所用字段。
        data = body if isinstance(body, bytes) else json.dumps(body).encode("utf-8") if body is not None else None
        request = urllib.request.Request(
            self.url + path, data=data, method=method,
            headers={"Content-Type": "application/x-ndjson" if isinstance(body, bytes) else "application/json"},
        )
        try:
            with outbound.open_request(request, timeout=self.timeout, follow_redirects=False) as response:
                result = json.load(response)
        except urllib.error.HTTPError as exc:
            with exc:
                if missing_ok and exc.code == 404:
                    return None
                if create and exc.code == 400:
                    try:
                        if json.load(exc)["error"]["type"] == "resource_already_exists_exception":
                            return {}
                    except (ValueError, KeyError, TypeError):
                        pass
            raise KeywordIndexUnavailable("关键词索引暂不可用") from None
        except (OSError, ValueError):
            raise KeywordIndexUnavailable("关键词索引暂不可用") from None
        if not isinstance(result, dict):
            raise KeywordIndexUnavailable("关键词索引响应无效")
        return result

    def missing_builds(self, builds: Sequence[UUID]) -> list[UUID]:
        missing: list[UUID] = []
        for offset in range(0, len(builds), 500):
            batch = builds[offset:offset + 500]
            markers = [f"build-{build}" for build in batch]
            response = self._request("POST", "/_mget", {"ids": markers}, missing_ok=True)
            try:
                docs = response["docs"] if response is not None else []
                if response is not None and {item["_id"] for item in docs} != set(markers):
                    raise ValueError
                # 多文档读取也可能用 HTTP 200 包装逐条的索引不存在错误。
                if response is None or all(
                    item.get("error", {}).get("type") == "index_not_found_exception" for item in docs
                ):
                    self._request("PUT", "", _INDEX, create=True)
                    missing.extend(batch)
                    continue
                found = {item["_id"]: item["found"] for item in docs}
                if not all(isinstance(value, bool) for value in found.values()):
                    raise ValueError
            except (KeyError, TypeError, AttributeError, ValueError):
                raise KeywordIndexUnavailable("关键词索引响应无效") from None
            missing.extend(build for build, marker in zip(batch, markers, strict=True) if not found[marker])
        return missing

    def _bulk(self, lines: list[str]) -> None:
        response = self._request("POST", "/_bulk", ("\n".join(lines) + "\n").encode("utf-8"))
        if response is None or response.get("errors") is not False:
            raise KeywordIndexUnavailable("关键词索引同步失败")

    def index_builds(
        self, builds: Mapping[UUID, UUID], chunks: Iterable[tuple[UUID, UUID, str]],
    ) -> None:
        entries = iter(chunks)
        while batch := list(islice(entries, 250)):
            lines: list[str] = []
            for build_id, key, content in batch:
                lines.append(json.dumps({"index": {"_id": str(key)}}))
                lines.append(json.dumps({
                    "key": str(key), "build_id": str(build_id), "document_id": str(builds[build_id]),
                    "content": content,
                }, ensure_ascii=False))
            self._bulk(lines)
        # 先刷新分块，再发布标记。中断后相同 ID 可覆盖重试，部分写入不会被当成已完成。
        response = self._request("POST", "/_refresh")
        if response is None or response.get("_shards", {}).get("failed") != 0:
            raise KeywordIndexUnavailable("关键词索引同步失败")
        lines = []
        for build_id, document_id in builds.items():
            lines.append(json.dumps({"index": {"_id": f"build-{build_id}"}}))
            lines.append(json.dumps({"document_id": str(document_id)}))
        self._bulk(lines)

    def search(self, query: str, builds: Sequence[UUID], limit: int) -> list[tuple[UUID, float]]:
        if not builds:
            return []
        response = self._request("POST", "/_search?allow_partial_search_results=false", {
            "query": {"bool": {
                "must": {"match": {"content": query}},
                "filter": {"terms": {"build_id": sorted(map(str, builds))}},
            }},
            "size": limit, "_source": False, "track_total_hits": False,
            "sort": [{"_score": "desc"}, {"key": "asc"}],
        })
        try:
            if response is None or response.get("timed_out") is not False:
                raise ValueError
            hits = [(UUID(hit["_id"]), float(hit["_score"])) for hit in response["hits"]["hits"]]
            if any(not math.isfinite(score) or score < 0 for _, score in hits):
                raise ValueError
            return hits
        except (KeyError, TypeError, ValueError, AttributeError):
            raise KeywordIndexUnavailable("关键词索引响应无效") from None

    def delete_document(self, document_id: UUID) -> None:
        # 清理也覆盖尚未刷新就失败的批次。
        if self._request("POST", "/_refresh", missing_ok=True) is None:
            return
        response = self._request("POST", "/_delete_by_query?refresh=true", {
            "query": {"term": {"document_id": str(document_id)}},
        }, missing_ok=True)
        if response is not None and (
            response.get("timed_out") is not False or response.get("failures")
            or response.get("version_conflicts") != 0
        ):
            raise KeywordIndexUnavailable("关键词索引清理失败")


def build_keyword_index(settings: Settings) -> OpenSearchKeywordIndex | None:
    if not settings.opensearch_url:
        return None
    return OpenSearchKeywordIndex(
        settings.opensearch_url, settings.opensearch_index, settings.opensearch_timeout_seconds,
    )
