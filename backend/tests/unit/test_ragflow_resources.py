"""资源预检独立于工作目录，并保留显式配置和离线校验。"""

import hashlib
import os

import nltk.data
import pytest

from knowledge.adapters import ragflow_text
from scripts import prepare_ragflow_resources, real_model


def _offline_environment(monkeypatch, tmp_path):
    environment = {
        key: value for key, value in os.environ.items()
        if key not in {"NLTK_DATA", "TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR"}
    }
    monkeypatch.setattr(os, "environ", environment)
    monkeypatch.setattr(nltk.data, "path", [])
    monkeypatch.setattr(real_model, "BACKEND", tmp_path)
    monkeypatch.setattr(ragflow_text, "count_tokens", lambda _: 1)
    monkeypatch.setattr(ragflow_text, "query_terms", lambda _: None)
    return environment


def test_preflight_sets_absolute_defaults_after_nltk_import(monkeypatch, tmp_path):
    env = _offline_environment(monkeypatch, tmp_path)
    monkeypatch.chdir(tmp_path.parent)
    real_model.check_ragflow_resources()
    root = tmp_path / ".data" / "ragflow-resources"
    assert env["NLTK_DATA"] == str(root)
    assert env["TIKTOKEN_CACHE_DIR"] == str(root / "tiktoken")
    assert nltk.data.path == [str(root)]


@pytest.mark.parametrize("cache_variable", ["TIKTOKEN_CACHE_DIR", "DATA_GYM_CACHE_DIR"])
def test_preflight_preserves_explicit_resource_paths(monkeypatch, tmp_path, cache_variable):
    env = _offline_environment(monkeypatch, tmp_path)
    first, second = tmp_path / "dictionary-a", tmp_path / "dictionary-b"
    env["NLTK_DATA"] = os.pathsep.join([str(first), str(second)])
    env[cache_variable] = str(tmp_path / "custom-token-cache")
    real_model.check_ragflow_resources()
    assert env[cache_variable] == str(tmp_path / "custom-token-cache")
    assert nltk.data.path == [str(first), str(second)]
    if cache_variable == "DATA_GYM_CACHE_DIR":
        assert "TIKTOKEN_CACHE_DIR" not in env


def test_preflight_identifies_invalid_token_vocabulary(monkeypatch, tmp_path):
    _offline_environment(monkeypatch, tmp_path)

    def broken(_):
        raise RuntimeError("synthetic corrupt cache")

    monkeypatch.setattr(ragflow_text, "count_tokens", broken)
    with pytest.raises(ValueError, match="cl100k_base 词表缺失或校验失败"):
        real_model.check_ragflow_resources()


def test_offline_token_preparation_validates_and_reuses_cache(monkeypatch, tmp_path):
    content = b"synthetic tokenizer vocabulary"
    monkeypatch.setattr(prepare_ragflow_resources, "_CL100K", (
        "https://example.invalid/vocabulary", len(content), hashlib.sha256(content).hexdigest(),
    ))

    def no_network(*args, **kwargs):
        pytest.fail("离线准备和有效缓存复用不得访问网络")

    monkeypatch.setattr(prepare_ragflow_resources.urllib.request, "urlopen", no_network)
    source = tmp_path / "offline-vocabulary"
    source.write_bytes(content)
    target = prepare_ragflow_resources.prepare_tiktoken(tmp_path / "resources", source)
    assert target.read_bytes() == content
    assert prepare_ragflow_resources.prepare_tiktoken(tmp_path / "resources") == target
    source.write_bytes(b"corrupted")
    with pytest.raises(ValueError, match="校验失败"):
        prepare_ragflow_resources.prepare_tiktoken(tmp_path / "other-resources", source)
