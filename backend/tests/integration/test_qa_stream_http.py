"""问答 SSE 的 HTTP 外层：路由装配、预检错误映射与事件帧（进程内，不启动独立服务）。

进程级 HTTP 验收见 tests/e2e/test_qa_stream.py；这里用依赖覆盖固定身份与模型桩，
验证路由层本身的契约，避免受外部进程启动时间影响。
"""

from __future__ import annotations

import json
import os

import pytest
from fastapi.testclient import TestClient

from knowledge.api.deps import (
    get_actor,
    get_embedder,
    get_generator,
    get_session,
    get_settings_dep,
)
from knowledge.domain.entities import Actor, GeneratedAnswer, GeneratedSection
from knowledge.entrypoints.api import create_app
from knowledge.services import conversations as conversation_service
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.worker.loop import process_next
from samples import MEETING_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


class _Generator:
    revision = "http-fixture"

    def generate(self, question, evidence, *, history=()):
        if not evidence:
            return GeneratedAnswer(sections=())
        return GeneratedAnswer(
            sections=(GeneratedSection(text="依据", evidence_keys=(evidence[0].key,)),),
            model_revision=self.revision,
        )

    def stream(self, question, evidence, *, history=()):
        if not evidence:
            return
        yield f"依据：{evidence[0].text[:40]}"
        yield " [E1]"


def _build_app(session, settings, deps, actor: Actor) -> TestClient:
    app = create_app()
    app.dependency_overrides[get_session] = lambda: session
    app.dependency_overrides[get_actor] = lambda: actor
    app.dependency_overrides[get_settings_dep] = lambda: settings
    app.dependency_overrides[get_embedder] = lambda: deps.embedder
    app.dependency_overrides[get_generator] = lambda: _Generator()
    return TestClient(app)


def _seed(session, settings, deps, actor: Actor):
    kb = knowledge_service.create(session, actor, "流式HTTP")
    document_service.upload(
        session, actor, settings, deps.storage, deps.registry,
        kb_id=kb.id, filename="meeting-room.md", data=MEETING_RULES,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps) is True
    return kb


def _events(response) -> list[tuple[str, dict]]:
    collected = []
    kind = None
    for line in response.iter_lines():
        if line.startswith("event: "):
            kind = line.removeprefix("event: ")
        elif line.startswith("data: "):
            collected.append((kind, json.loads(line.removeprefix("data: "))))
        elif line == "":
            kind = None
    return collected


def test_stream_endpoint_returns_sse_with_validated_citations(session, settings, deps):
    actor = Actor(subject_id="http-stream", tenant_id="default")
    kb = _seed(session, settings, deps, actor)
    conversation = conversation_service.create(session, actor, "HTTP 流式")
    client = _build_app(session, settings, deps, actor)

    body = {
        "question": "会议室使用后需要做什么",
        "knowledge_base_ids": [str(kb.id)],
        "conversation_id": str(conversation.id),
        "stream": True,
    }
    with client.stream("POST", "/api/v1/qa", json=body) as response:
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("text/event-stream")
        assert response.headers["x-accel-buffering"] == "no"
        assert response.headers["x-request-id"]
        events = _events(response)

    names = [name for name, _ in events]
    assert names[0] == "run_started"
    assert names[-2:] == ["citations", "completed"]
    assert dict(events)["citations"]["citations"][0]["index"] == 1

    _conversation, messages = conversation_service.detail(session, actor, conversation.id)
    assert [item.role for item in messages] == ["user", "assistant"]
    assert messages[1].citations


def test_stream_endpoint_preflight_errors_are_json(session, settings, deps):
    actor = Actor(subject_id="http-stream-preflight", tenant_id="default")
    other = Actor(subject_id="http-stream-other", tenant_id="default")
    kb = _seed(session, settings, deps, actor)
    conversation = conversation_service.create(session, actor, "他人会话")
    client = _build_app(session, settings, deps, other)

    response = client.post(
        "/api/v1/qa",
        json={
            "question": "会议室使用后需要做什么",
            "knowledge_base_ids": [str(kb.id)],
            "conversation_id": str(conversation.id),
            "stream": True,
        },
    )
    assert response.status_code == 404
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "RESOURCE_NOT_FOUND"


def test_non_stream_endpoint_still_returns_envelope(session, settings, deps):
    actor = Actor(subject_id="http-json", tenant_id="default")
    kb = _seed(session, settings, deps, actor)
    conversation = conversation_service.create(session, actor, "JSON")
    client = _build_app(session, settings, deps, actor)

    response = client.post(
        "/api/v1/qa",
        json={
            "question": "会议室使用后需要做什么",
            "knowledge_base_ids": [str(kb.id)],
            "conversation_id": str(conversation.id),
        },
    )
    assert response.status_code == 200
    data = response.json()["data"]
    assert data["answer_status"] == "answered"
    assert data["citations"]
    assert data["retrieval"]["context_messages"] == 0
    assert data["retrieval"]["query_rewritten"] is False
