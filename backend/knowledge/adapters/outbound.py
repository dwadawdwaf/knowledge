"""对外 HTTP 出口。

本机端点直连，绕过系统代理：否则 `HTTP_PROXY` 会把 `127.0.0.1` 的模型服务
也交给代理处理，表现为 `MODEL_UNAVAILABLE`，与「服务没启动」难以区分。
"""

from __future__ import annotations

import urllib.request
from functools import lru_cache
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


@lru_cache(maxsize=8)
def _build(direct: bool) -> urllib.request.OpenerDirector:
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def uses_direct_connection(base_url: str) -> bool:
    """端点是否本机；本机必须直连，否则会被系统代理截走。"""
    return (urlsplit(base_url).hostname or "") in LOOPBACK_HOSTS


def opener_for(base_url: str) -> urllib.request.OpenerDirector:
    """按端点是否本机返回对应 opener。"""
    return _build(uses_direct_connection(base_url))


def open_request(request: urllib.request.Request, *, timeout: float):
    """唯一出口：适配器都经这里发请求，便于替换与代理策略集中管理。"""
    return opener_for(request.full_url).open(request, timeout=timeout)
