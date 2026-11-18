"""真实 HTTP 验证问答 SSE：阶段顺序、增量正文、中途故障与客户端断开。"""

from __future__ import annotations

import json
import time

import httpx

from scripts.http_scenario import expect, upload_sample, wait_job

# 问答是长连接：首帧要等上下文与检索开始，慢机器上按默认请求超时会误判为失败。
SSE_TIMEOUT = httpx.Timeout(90.0)

STAGE_PREFIX = [
    "run_started",
    "context_started",
    "context_completed",
    "query_rewrite_started",
    "query_rewrite_completed",
    "retrieval_started",
    "retrieval_completed",
    "generation_started",
]


def read_events(lines):
    """把命名 SSE 帧解析成 (event, data)；空行结束一帧。"""
    kind = None
    data = None
    for line in lines:
        if line.startswith("event: "):
            kind = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data = json.loads(line.removeprefix("data: "))
        elif line == "" and kind:
            yield kind, data
            kind, data = None, None


def _ready(runtime, client, name: str) -> str:
    kb = expect(client.post("/api/v1/knowledge-bases", json={"name": name}), 201)
    uploaded = upload_sample(client, kb["id"])
    wait_job(client, uploaded["job_id"], progress=runtime.say)
    return kb["id"]


def _conversation(client, title: str) -> str:
    return expect(client.post("/api/v1/conversations", json={"title": title}), 201)["id"]


def _ask(client, kb_id, question, conversation_id):
    return {
        "question": question,
        "knowledge_base_ids": [kb_id],
        "conversation_id": conversation_id,
        "stream": True,
    }


def test_stream_emits_stage_order_and_validated_citations(runtime):
    with runtime.client() as client:
        kb_id = _ready(runtime, client, "问答流式")
        conversation_id = _conversation(client, "流式")
        with client.stream("POST", "/api/v1/qa", timeout=SSE_TIMEOUT, json=_ask(client, kb_id, "会议室使用后需要做什么？", conversation_id)) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            assert response.headers["x-accel-buffering"] == "no"
            collected = list(read_events(response.iter_lines()))

        names = [kind for kind, _ in collected]
        assert names[: len(STAGE_PREFIX)] == STAGE_PREFIX
        assert names[-2:] == ["citations", "completed"]
        # 答案确实是分块到达的，不是一次性返回
        assert names.count("answer_delta") >= 2

        payload = {kind: data for kind, data in collected}
        assert payload["context_completed"]["messages"] == 0
        assert payload["context_completed"]["limit"] == 12
        assert payload["query_rewrite_completed"]["rewritten"] is False
        assert payload["retrieval_completed"]["hits"] > 0
        assert payload["citations"]["citations"][0]["index"] == 1
        assert payload["completed"]["answer_status"] == "answered"

        deltas = "".join(data["text"] for kind, data in collected if kind == "answer_delta")
        assert "[E1]" not in deltas
        assert deltas.strip() == payload["completed"]["answer"].strip()

        detail = expect(client.get(f"/api/v1/conversations/{conversation_id}"), 200)
        assert [item["role"] for item in detail["messages"]] == ["user", "assistant"]
        assert detail["messages"][1]["body"] == payload["completed"]["answer"]
        assert detail["messages"][1]["citations"]
        runtime.say("[stream] PASS: 阶段顺序、增量正文、校验后引用与存档一致")


def test_second_turn_uses_conversation_context(runtime):
    with runtime.client() as client:
        kb_id = _ready(runtime, client, "问答上下文")
        conversation_id = _conversation(client, "多轮")
        first = _ask(client, kb_id, "会议室使用后需要做什么？", conversation_id)
        with client.stream("POST", "/api/v1/qa", timeout=SSE_TIMEOUT, json=first) as response:
            assert [kind for kind, _ in read_events(response.iter_lines())][-1] == "completed"

        second = _ask(client, kb_id, "它还需要提前登记吗？", conversation_id)
        with client.stream("POST", "/api/v1/qa", timeout=SSE_TIMEOUT, json=second) as response:
            collected = list(read_events(response.iter_lines()))

        payload = {kind: data for kind, data in collected}
        assert payload["context_completed"]["messages"] == 2
        assert payload["query_rewrite_completed"]["rewritten"] is True
        assert "会议室使用后需要做什么" in payload["query_rewrite_completed"]["query"]
        assert payload["completed"]["answer_status"] == "answered"
        runtime.say("[stream] PASS: 第二轮把上一轮问答带入上下文与检索问题")


def test_midstream_upstream_failure_reports_error_event(runtime):
    with runtime.client() as client:
        kb_id = _ready(runtime, client, "问答中途故障")
        conversation_id = _conversation(client, "中途故障")
        runtime.model_fixture.generator_failure = "stream-error"
        with client.stream("POST", "/api/v1/qa", timeout=SSE_TIMEOUT, json=_ask(client, kb_id, "会议室使用后需要做什么？", conversation_id)) as response:
            assert response.status_code == 200
            collected = list(read_events(response.iter_lines()))

        names = [kind for kind, _ in collected]
        assert "completed" not in names
        assert names[-1] == "error"
        assert dict(collected)["error"]["code"] == "MODEL_UNAVAILABLE"
        # 未完成校验的答案不落库
        detail = expect(client.get(f"/api/v1/conversations/{conversation_id}"), 200)
        assert detail["messages"] == []
        runtime.say("[stream] PASS: 上游中途断流 -> error 事件，不写入半段答案")


def test_client_disconnect_stops_run_and_keeps_conversation_clean(runtime):
    with runtime.client() as client:
        kb_id = _ready(runtime, client, "问答断开")
        conversation_id = _conversation(client, "断开")
        runtime.model_fixture.generator_failure = "stream-slow"
        with client.stream("POST", "/api/v1/qa", timeout=SSE_TIMEOUT, json=_ask(client, kb_id, "会议室使用后需要做什么？", conversation_id)) as response:
            assert response.status_code == 200
            seen = []
            for kind, _data in read_events(response.iter_lines()):
                seen.append(kind)
                if len(seen) >= 4:
                    break

        # 断开后服务端应停止本轮：会话里不留下未完成的答案
        deadline = time.monotonic() + 10
        while time.monotonic() < deadline:
            detail = expect(client.get(f"/api/v1/conversations/{conversation_id}"), 200)
            if detail["messages"] == []:
                break
            time.sleep(0.2)
        assert detail["messages"] == []
        runtime.model_fixture.reset()
        # 连接中断不影响后续请求
        answer = expect(
            client.post(
                "/api/v1/qa",
                json={"question": "会议室使用后需要做什么？", "knowledge_base_ids": [kb_id]},
            ),
            200,
        )
        assert answer["answer_status"] == "answered"
        runtime.say("[stream] PASS: 客户端断开后停止生成，服务仍可继续回答")


def test_stream_preflight_errors_use_json_envelope(runtime):
    with runtime.client() as client:
        kb_id = _ready(runtime, client, "问答预检")
        missing = "00000000-0000-0000-0000-000000000000"
        bad_conversation = client.post(
            "/api/v1/qa",
            json=_ask(client, kb_id, "会议室使用后需要做什么？", missing),
        )
        assert bad_conversation.status_code == 404
        assert bad_conversation.json()["error"]["code"] == "RESOURCE_NOT_FOUND"

        other_kb = client.post(
            "/api/v1/qa",
            json={"question": "会议室使用后需要做什么？", "knowledge_base_ids": [missing], "stream": True},
        )
        assert other_kb.status_code == 404
        assert other_kb.headers["content-type"].startswith("application/json")
