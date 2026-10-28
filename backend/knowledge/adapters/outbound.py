"""对外 HTTP 出口。

本机端点直连，绕过系统代理：否则 `HTTP_PROXY` 会把 `127.0.0.1` 的模型服务
也交给代理处理，表现为 `MODEL_UNAVAILABLE`，与「服务没启动」难以区分。

远端端点默认沿用环境代理，但**代理本身没在监听时改直连**：这台机器常开着
本地代理（退出后地址仍留在环境变量里），此时所有远端请求都会以
`ConnectionRefused` 失败，现象与「模型服务不可达」完全相同。代理不可用不等于
目标不可达，因此只在确认代理端口已关闭时绕过，并记一条警告，不静默改变行为。
"""

from __future__ import annotations

import logging
import os
import socket
import urllib.request
from functools import lru_cache
from urllib.parse import urlsplit

LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
logger = logging.getLogger("knowledge.outbound")

_warned: set[str] = set()


def _env_proxy() -> str | None:
    """读取环境里的 HTTP(S) 代理地址；不读注册表，避免受系统 IE 设置影响。"""
    for name in ("https_proxy", "HTTPS_PROXY", "http_proxy", "HTTP_PROXY", "all_proxy"):
        value = os.environ.get(name)
        if value:
            return value
    return None


def proxy_port_closed(proxy: str, *, timeout: float = 0.3) -> bool:
    """代理地址解析到本机且端口未监听时返回 True；远端代理一律返回 False。"""
    parsed = urlsplit(proxy if "://" in proxy else f"http://{proxy}")
    host = parsed.hostname or ""
    if host not in LOOPBACK_HOSTS:
        return False
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return False
    except OSError:
        return True


def dead_local_proxy() -> str | None:
    """环境里配置了本机代理但端口已关闭时，返回该代理地址。"""
    proxy = _env_proxy()
    if proxy and proxy_port_closed(proxy):
        return proxy
    return None


@lru_cache(maxsize=8)
def _build(direct: bool) -> urllib.request.OpenerDirector:
    if direct:
        return urllib.request.build_opener(urllib.request.ProxyHandler({}))
    return urllib.request.build_opener()


def uses_direct_connection(base_url: str) -> bool:
    """端点是否本机；本机必须直连，否则会被系统代理截走。"""
    return (urlsplit(base_url).hostname or "") in LOOPBACK_HOSTS


def opener_for(base_url: str) -> urllib.request.OpenerDirector:
    """按端点是否本机、代理是否可用返回对应 opener。"""
    if uses_direct_connection(base_url):
        return _build(True)
    dead = dead_local_proxy()
    if dead is not None:
        if dead not in _warned:
            _warned.add(dead)
            logger.warning("proxy_unreachable", extra={"proxy": dead, "fallback": "direct"})
        return _build(True)
    return _build(False)


def open_request(request: urllib.request.Request, *, timeout: float):
    """唯一出口：适配器都经这里发请求，便于替换与代理策略集中管理。"""
    return opener_for(request.full_url).open(request, timeout=timeout)
