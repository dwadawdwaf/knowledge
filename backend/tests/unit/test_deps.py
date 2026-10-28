"""出口策略与依赖探测的纯函数行为，不访问真实网络。"""

from __future__ import annotations

from knowledge.adapters import outbound
from scripts.deps import _local_service, _matches


def test_loopback_endpoint_is_direct():
    """本机端点必须绕过代理，否则本地模型服务会被代理截走。"""
    assert outbound.uses_direct_connection("http://127.0.0.1:11434/v1")
    assert outbound.uses_direct_connection("http://localhost:11434/v1")


def test_remote_endpoint_uses_system_route():
    assert not outbound.uses_direct_connection("https://api.deepseek.com")


def test_open_request_picks_opener_by_host():
    direct = outbound.opener_for("http://127.0.0.1:11434/v1")
    remote = outbound.opener_for("https://api.deepseek.com")
    assert direct is not remote


def test_local_service_detection():
    assert _local_service("http://localhost:11434/v1", 11434) == ("127.0.0.1", 11434)
    assert _local_service("http://127.0.0.1/v1", 11434) == ("127.0.0.1", 11434)
    assert _local_service("https://api.deepseek.com", 11434) is None


def test_model_tag_matching_ignores_latest_suffix():
    assert _matches("bge-m3", ["bge-m3:latest", "qwen2.5:7b"])
    assert _matches("bge-m3:latest", ["bge-m3:latest"])
    assert not _matches("bge-m3", ["qwen2.5:7b"])


def test_ensure_database_skips_remote_host():
    from knowledge.config import Settings
    from scripts.deps import ensure_database

    settings = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@db.example.com:5432/knowledge")
    ensure_database(settings, progress=lambda message: None)  # 远端不归启动器管


def test_ensure_database_requires_tunnel_when_port_closed():
    import pytest

    from knowledge.config import Settings
    from scripts.deps import DependencyError, ensure_database

    settings = Settings(_env_file=None, database_url="postgresql+psycopg://u:p@127.0.0.1:1/knowledge")
    with pytest.raises(DependencyError):
        ensure_database(settings, progress=lambda message: None)


def test_dead_local_proxy_is_detected(monkeypatch):
    from knowledge.adapters import outbound

    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setattr(outbound, "proxy_port_closed", lambda proxy, timeout=0.3: True)
    assert outbound.dead_local_proxy() == "http://127.0.0.1:9"


def test_remote_proxy_is_never_treated_as_dead(monkeypatch):
    from knowledge.adapters import outbound

    monkeypatch.setenv("https_proxy", "http://203.0.113.10:8080")
    assert outbound.dead_local_proxy() is None


def test_remote_endpoint_falls_back_to_direct_when_proxy_closed(monkeypatch):
    from knowledge.adapters import outbound

    monkeypatch.setenv("https_proxy", "http://127.0.0.1:9")
    monkeypatch.setattr(outbound, "proxy_port_closed", lambda proxy, timeout=0.3: True)
    built: list[bool] = []
    monkeypatch.setattr(outbound, "_build", lambda direct: built.append(direct) or object())
    outbound.opener_for("https://api.example.com/v1")
    assert built == [True]


def test_remote_endpoint_keeps_proxy_when_it_is_alive(monkeypatch):
    from knowledge.adapters import outbound

    monkeypatch.setenv("https_proxy", "http://127.0.0.1:7890")
    monkeypatch.setattr(outbound, "proxy_port_closed", lambda proxy, timeout=0.3: False)
    built: list[bool] = []
    monkeypatch.setattr(outbound, "_build", lambda direct: built.append(direct) or object())
    outbound.opener_for("https://api.example.com/v1")
    assert built == [False]


def test_network_error_names_host_and_reason():
    import urllib.error

    from scripts.deps import _describe_network_error

    exc = urllib.error.URLError(ConnectionRefusedError(10061, "refused"))
    message = _describe_network_error(exc, "生成服务", "https://api.example.com/chat/completions")
    assert "api.example.com" in message
    assert "ConnectionRefusedError" in message
