"""可选本机 /rerank 协议适配器；不下载模型，不允许跳转到远端。"""

from __future__ import annotations

import json
import math
import urllib.error
import urllib.request
from collections.abc import Sequence
from urllib.parse import urlsplit

from knowledge.config import Settings
from knowledge.domain.errors import ModelTimeout, ModelUnavailable
from knowledge.domain.ports import Reranker


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: object,
        code: int,
        msg: str,
        headers: object,
        newurl: str,
    ) -> None:
        raise ModelUnavailable("重排序服务禁止重定向")


class HttpReranker:
    def __init__(self, url: str, model: str, timeout: float = 20) -> None:
        parts = urlsplit(url)
        if (
            parts.scheme != "http"
            or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username
            or parts.password
            or parts.query
            or parts.fragment
        ):
            raise ModelUnavailable("重排序服务必须使用本机HTTP地址")
        self.url, self.model, self.timeout = url, model, timeout

    def rank(self, query: str, texts: Sequence[str]) -> list[float]:
        if not texts:
            return []
        request = urllib.request.Request(
            self.url,
            data=json.dumps(
                {"model": self.model, "query": query, "documents": list(texts)}
            ).encode(),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect())
            with opener.open(request, timeout=self.timeout) as response:
                data = response.read(1024 * 1024 + 1)
            if len(data) > 1024 * 1024:
                raise ModelUnavailable("重排序响应超限")
            results = json.loads(data)["results"]
            if len(results) != len(texts) or sorted(item["index"] for item in results) != list(
                range(len(texts))
            ):
                raise ValueError("indices")
            if any(
                type(item["index"]) is not int or type(item["relevance_score"]) not in {int, float}
                for item in results
            ):
                raise ValueError("types")
            scores = [
                float(item["relevance_score"])
                for item in sorted(results, key=lambda item: item["index"])
            ]
            if not all(math.isfinite(score) for score in scores):
                raise ValueError("scores")
            return scores
        except TimeoutError as exc:
            raise ModelTimeout("重排序服务超时") from exc
        except (OSError, ValueError, KeyError, TypeError, OverflowError) as exc:
            raise ModelUnavailable("重排序服务不可用或响应无效") from exc


def build_reranker(settings: Settings) -> Reranker | None:
    if not settings.reranker_url:
        return None
    return HttpReranker(
        settings.reranker_url, settings.reranker_model, settings.reranker_timeout_seconds
    )
