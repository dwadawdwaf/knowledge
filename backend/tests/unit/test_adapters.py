"""HTTP 适配器：鉴权头与维度参数下发（不访问真实端点）。"""

from __future__ import annotations

import io
import json
import urllib.request
from unittest import mock

import pytest

from knowledge.adapters.embedding import HttpEmbedder, build_embedder
from knowledge.adapters.generator import HttpGenerator, build_generator
from knowledge.config import Settings
from knowledge.domain.errors import CitationValidationFailed, ModelTimeout, ModelUnavailable


def _fake_urlopen(response_body: dict):
    """捕获 Request 并返回固定响应体。"""

    def _open(request, timeout=30):
        captured.append(request)
        return io.BytesIO(json.dumps(response_body).encode("utf-8"))

    captured: list[urllib.request.Request] = []
    return captured, _open


def test_embedder_sends_bearer_and_dimensions():
    captured, fake = _fake_urlopen({"data": [{"embedding": [0.1, 0.2]} for _ in range(2)]})
    embedder = HttpEmbedder(
        base_url="https://models.example/v4",
        model="bge-m3",
        dimension=2,
        api_key="sk-test",
        dimensions=1024,
    )
    with mock.patch.object(urllib.request, "urlopen", fake):
        vectors = embedder.embed(["文本一", "文本二"])

    assert len(vectors) == 2
    request = captured[0]
    assert request.full_url == "https://models.example/v4/embeddings"
    assert request.headers["Authorization"] == "Bearer sk-test"
    body = json.loads(request.data.decode("utf-8"))
    assert body["dimensions"] == 1024
    assert body["model"] == "bge-m3"


def test_embedder_omits_auth_and_dimensions_when_unset():
    captured, fake = _fake_urlopen({"data": [{"embedding": [0.1]}]})
    embedder = HttpEmbedder(base_url="https://models.example", model="m", dimension=1)
    with mock.patch.object(urllib.request, "urlopen", fake):
        embedder.embed(["文本"])

    assert "Authorization" not in captured[0].headers
    assert "dimensions" not in json.loads(captured[0].data.decode("utf-8"))


def test_embedder_rejects_wrong_dimension():
    _, fake = _fake_urlopen({"data": [{"embedding": [0.1, 0.2, 0.3]}]})
    embedder = HttpEmbedder(base_url="https://models.example", model="m", dimension=2)
    with mock.patch.object(urllib.request, "urlopen", fake), pytest.raises(Exception) as exc_info:
        embedder.embed(["文本"])
    assert "维度" in str(exc_info.value)


def test_generator_sends_bearer():
    captured, fake = _fake_urlopen({"choices": [{"message": {"content": '{"sections": []}'}}]})
    generator = HttpGenerator(
        base_url="https://models.example/v4", model="glm-4-flash", api_key="sk-test"
    )
    from knowledge.domain.entities import Evidence

    evidence = Evidence(
        key="E1",
        chunk_id="c",
        document_id="d",
        version_id="v",
        build_id="b",
        title="t",
        text="正文",
        locator={},
    )
    with mock.patch.object(urllib.request, "urlopen", fake):
        generator.generate("问题", [evidence])

    request = captured[0]
    assert request.full_url == "https://models.example/v4/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"


def test_build_adapters_wire_api_key():
    settings = Settings(
        embedding_base_url="https://e.example",
        embedding_api_key="sk-e",
        generator_base_url="https://g.example",
        generator_api_key="sk-g",
    )
    assert isinstance(build_embedder(settings), HttpEmbedder)
    assert isinstance(build_generator(settings), HttpGenerator)
    assert build_embedder(settings).api_key == "sk-e"
    assert build_generator(settings).api_key == "sk-g"


@pytest.mark.parametrize(
    "payload",
    [
        [],
        None,
        {},
        {"data": []},
        {"data": [{"embedding": [True]}]},
        {"data": [{"embedding": [float("nan")]}]},
        {"data": [{"index": 2, "embedding": [1.0]}]},
    ],
)
def test_embedder_invalid_response_is_model_unavailable(payload):
    _, fake = _fake_urlopen(payload)
    embedder = HttpEmbedder(base_url="http://fixture.invalid", model="m", dimension=1)
    with mock.patch.object(urllib.request, "urlopen", fake), pytest.raises(ModelUnavailable):
        embedder.embed(["synthetic"])


def test_embedder_sorts_response_indices_and_checks_count():
    _, fake = _fake_urlopen(
        {
            "data": [
                {"index": 1, "embedding": [2.0]},
                {"index": 0, "embedding": [1.0]},
            ]
        }
    )
    embedder = HttpEmbedder(base_url="http://fixture.invalid", model="m", dimension=1)
    with mock.patch.object(urllib.request, "urlopen", fake):
        assert embedder.embed(["first", "second"]) == [[1.0], [2.0]]
        with pytest.raises(ModelUnavailable):
            embedder.embed(["only-one"])


@pytest.mark.parametrize(
    "content",
    [
        "[]",
        "null",
        '{"sections":"bad"}',
        '{"sections":[{"text":123}]}',
        '{"sections":[{"text":"x","evidence_keys":"E1"}]}',
    ],
)
def test_generator_malformed_sections_are_citation_error(content):
    _, fake = _fake_urlopen({"choices": [{"message": {"content": content}}]})
    generator = HttpGenerator(base_url="http://fixture.invalid", model="m")
    with (
        mock.patch.object(urllib.request, "urlopen", fake),
        pytest.raises(CitationValidationFailed),
    ):
        generator.generate("synthetic", [])


@pytest.mark.parametrize(
    "adapter",
    [
        HttpEmbedder(base_url="http://fixture.invalid", model="m", dimension=1),
        HttpGenerator(base_url="http://fixture.invalid", model="m"),
    ],
)
def test_upstream_timeout_is_model_timeout(adapter):
    with (
        mock.patch.object(urllib.request, "urlopen", side_effect=TimeoutError),
        pytest.raises(ModelTimeout),
    ):
        if isinstance(adapter, HttpEmbedder):
            adapter.embed(["synthetic"])
        else:
            adapter.generate("synthetic", [])
