"""生成适配器的流式契约：请求负载、增量解析与故障分类（不访问真实端点）。"""

from __future__ import annotations

import json
import urllib.error
from typing import Self
from unittest import mock

import pytest

from knowledge.adapters import outbound
from knowledge.adapters.generator import (
    STREAM_SYSTEM_PROMPT,
    HttpGenerator,
    StubGenerator,
)
from knowledge.domain.entities import ContextMessage, Evidence
from knowledge.domain.errors import ModelTimeout, ModelUnavailable

EVIDENCE = [
    Evidence(
        key="E1",
        chunk_id="chunk-1",
        document_id="doc",
        version_id="version",
        build_id="build",
        title="资料",
        text="会议室使用后关闭照明。",
        locator={},
    )
]

HISTORY = [
    ContextMessage(role="user", text="上一轮问题"),
    ContextMessage(role="assistant", text="上一轮回答"),
]


class _FakeResponse:
    def __init__(self, frames: list[str]) -> None:
        self._frames = [frame.encode("utf-8") for frame in frames]

    def __iter__(self):
        return iter(self._frames)

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def read(self) -> bytes:  # 非流式调用使用
        return b"".join(self._frames)


def _sse(*contents: str) -> list[str]:
    frames = [
        f"data: {json.dumps({'choices': [{'delta': {'content': content}}]})}\n" for content in contents
    ]
    return [*frames, "data: [DONE]\n"]


def _capture(frames: list[str]):
    captured = []

    def _open(request, timeout=30):
        captured.append(request)
        return _FakeResponse(frames)

    return captured, _open


def test_stream_sends_history_and_marks_streaming_request():
    captured, fake = _capture(_sse("答", "案"))
    generator = HttpGenerator(base_url="http://fixture.invalid/v1", model="m")
    with mock.patch.object(outbound, "open_request", fake):
        assert "".join(generator.stream("当前问题", EVIDENCE, history=HISTORY)) == "答案"

    body = json.loads(captured[0].data.decode("utf-8"))
    assert body["stream"] is True
    assert body["messages"][0] == {"role": "system", "content": STREAM_SYSTEM_PROMPT}
    assert body["messages"][1:3] == [
        {"role": "user", "content": "上一轮问题"},
        {"role": "assistant", "content": "上一轮回答"},
    ]
    # 历史在前，本轮证据与问题仍是最后一条 user 消息
    assert body["messages"][-1]["role"] == "user"
    assert "[E1] 会议室使用后关闭照明。" in body["messages"][-1]["content"]
    assert body["messages"][-1]["content"].endswith("问题：当前问题")


def test_stream_ignores_blank_lines_comments_and_done():
    frames = [
        "\n",
        ": keep-alive\n",
        'data: {"choices":[{"delta":{"role":"assistant","content":null}}]}\n',
        'data: {"choices":[{"delta":{"content":"正文"}}]}\n',
        "data: [DONE]\n",
        'data: {"choices":[{"delta":{"content":"不应到达"}}]}\n',
    ]
    _, fake = _capture(frames)
    generator = HttpGenerator(base_url="http://fixture.invalid", model="m")
    with mock.patch.object(outbound, "open_request", fake):
        assert list(generator.stream("问题", EVIDENCE)) == ["正文"]


def test_malformed_chunk_is_model_unavailable():
    _, fake = _capture(['data: {"choices": [\n'])
    generator = HttpGenerator(base_url="http://fixture.invalid", model="m")
    with mock.patch.object(outbound, "open_request", fake), pytest.raises(ModelUnavailable):
        list(generator.stream("问题", EVIDENCE))


@pytest.mark.parametrize(
    ("failure", "expected"),
    [
        (TimeoutError(), ModelTimeout),
        (urllib.error.URLError(ConnectionRefusedError()), ModelUnavailable),
        (urllib.error.HTTPError("http://fixture.invalid", 503, "", {}, None), ModelUnavailable),
    ],
)
def test_stream_failures_are_classified(failure, expected):
    generator = HttpGenerator(base_url="http://fixture.invalid", model="m")
    with (
        mock.patch.object(outbound, "open_request", side_effect=failure),
        pytest.raises(expected),
    ):
        list(generator.stream("问题", EVIDENCE))


def test_non_stream_generate_still_sends_history():
    captured, fake = _capture(
        [json.dumps({"choices": [{"message": {"content": '{"sections": []}'}}]})]
    )
    generator = HttpGenerator(base_url="http://fixture.invalid", model="m")
    with mock.patch.object(outbound, "open_request", fake):
        generator.generate("当前问题", EVIDENCE, history=HISTORY)

    body = json.loads(captured[0].data.decode("utf-8"))
    assert "stream" not in body
    assert len(body["messages"]) == 4
    assert body["messages"][1]["content"] == "上一轮问题"


def test_stub_stream_emits_marker_and_matches_non_stream_text():
    stub = StubGenerator()
    delta = "".join(stub.stream("问题", EVIDENCE, history=HISTORY))
    assert delta.endswith("[E1]")
    generated = stub.generate("问题", EVIDENCE, history=HISTORY)
    assert generated.sections[0].text in delta


def test_stub_stream_is_empty_without_evidence():
    assert list(StubGenerator().stream("问题", [])) == []
