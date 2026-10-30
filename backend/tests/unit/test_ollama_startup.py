"""依赖启动使用配置中的端口；合成进程回执，不加载模型或修改系统。"""

import pathlib
from unittest.mock import Mock

import pytest

from scripts import deps


def test_start_passes_explicit_host_and_preserves_model_directory(monkeypatch, tmp_path):
    monkeypatch.setattr(deps, "BACKEND", tmp_path)
    monkeypatch.setenv("OLLAMA_HOST", "0.0.0.0:11434")
    monkeypatch.setenv("OLLAMA_MODELS", "D:/synthetic-models")
    start = Mock()
    monkeypatch.setattr(deps.subprocess, "Popen", start)
    process = deps._start_ollama(pathlib.Path("synthetic/ollama.exe"), "127.0.0.1", 21434)
    assert process is start.return_value
    env = start.call_args.kwargs["env"]
    assert env["OLLAMA_HOST"] == "127.0.0.1:21434"
    assert env["OLLAMA_MODELS"] == "D:/synthetic-models"
    assert deps.os.environ["OLLAMA_HOST"] == "0.0.0.0:11434"
    assert deps._ollama_env("::1", 21434)["OLLAMA_HOST"] == "[::1]:21434"


def test_blocked_port_fails_before_start(monkeypatch):
    probe = Mock()
    probe.__enter__ = Mock(return_value=probe)
    probe.__exit__ = Mock(return_value=False)
    probe.bind.side_effect = PermissionError(10013, "synthetic")
    monkeypatch.setattr(deps.socket, "socket", Mock(return_value=probe))
    with pytest.raises(deps.DependencyError, match="系统保留"):
        deps._check_bindable("127.0.0.1", 11434)


def prepare(monkeypatch):
    monkeypatch.setattr(deps, "find_ollama", lambda: pathlib.Path("synthetic.exe"))
    monkeypatch.setattr(deps, "_check_bindable", Mock())
    monkeypatch.setattr(deps.time, "sleep", Mock())
    process = Mock(returncode=1)
    process.poll.return_value = 1
    start = Mock(return_value=process)
    monkeypatch.setattr(deps, "_start_ollama", start)
    return start


def test_exited_process_is_not_reported_as_timeout(monkeypatch):
    start = prepare(monkeypatch)
    monkeypatch.setattr(deps, "_port_open", lambda *args: False)
    with pytest.raises(deps.DependencyError, match="进程已退出"):
        deps.ensure_ollama("http://127.0.0.1:21434/v1", "synthetic")
    start.assert_called_once_with(pathlib.Path("synthetic.exe"), "127.0.0.1", 21434)


def test_configured_port_is_used_for_start_and_model_check(monkeypatch):
    start = prepare(monkeypatch)
    monkeypatch.setattr(deps, "_port_open", Mock(side_effect=[False, True]))
    tags = Mock(return_value=["synthetic:latest"])
    monkeypatch.setattr(deps, "_list_models", tags)
    deps.ensure_ollama("http://localhost:21434/v1", "synthetic")
    start.assert_called_once_with(pathlib.Path("synthetic.exe"), "127.0.0.1", 21434)
    tags.assert_called_once_with("127.0.0.1", 21434)


def test_pull_uses_same_port_and_requires_opt_in(monkeypatch):
    prepare(monkeypatch)
    monkeypatch.setattr(deps, "_port_open", lambda *args: True)
    monkeypatch.setattr(deps, "_list_models", lambda *args: [])
    pull = Mock(return_value=Mock(returncode=0))
    monkeypatch.setattr(deps.subprocess, "run", pull)
    with pytest.raises(deps.DependencyError, match="未安装"):
        deps.ensure_ollama("http://127.0.0.1:21434/v1", "synthetic")
    pull.assert_not_called()
    deps.ensure_ollama("http://127.0.0.1:21434/v1", "synthetic", pull_missing=True)
    assert pull.call_args.kwargs["env"]["OLLAMA_HOST"] == "127.0.0.1:21434"


def test_remote_endpoint_does_not_start_or_pull(monkeypatch):
    start = prepare(monkeypatch)
    deps.ensure_ollama("https://synthetic.invalid/v1", "synthetic")
    start.assert_not_called()
