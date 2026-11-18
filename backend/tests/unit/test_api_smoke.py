"""接口外壳的冒烟测试：不连接数据库，验证鉴权与错误映射。"""

from __future__ import annotations

import uuid

from fastapi.testclient import TestClient

from knowledge.config import get_settings
from knowledge.entrypoints.api import create_app
from knowledge.security import issue_token


def _client() -> TestClient:
    return TestClient(create_app())


def test_missing_token_rejected():
    response = _client().get("/api/v1/knowledge-bases")
    assert response.status_code == 401
    body = response.json()
    assert body["error"]["code"] == "UNAUTHENTICATED"
    assert body["error"]["retryable"] is False
    assert body["meta"]["request_id"]


def test_invalid_token_rejected():
    response = _client().get(
        "/api/v1/knowledge-bases", headers={"Authorization": "Bearer broken.token"}
    )
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_malformed_base64_returns_401():
    response = _client().get("/api/v1/knowledge-bases", headers={"Authorization": "Bearer a.x"})
    assert response.status_code == 401
    assert response.json()["error"]["code"] == "UNAUTHENTICATED"


def test_request_id_echoed():
    response = _client().get("/api/v1/knowledge-bases", headers={"X-Request-Id": "trace-123"})
    assert response.headers["X-Request-Id"] == "trace-123"
    assert response.json()["meta"]["request_id"] == "trace-123"


def test_stream_preflight_uses_json_error_envelope():
    """stream=true 在建立事件流前完成参数校验，错误仍按统一 JSON 信封返回。"""
    token = issue_token("user-a", settings=get_settings())
    response = _client().post(
        "/api/v1/qa",
        headers={"Authorization": f"Bearer {token}"},
        json={"question": "使用后要做什么", "knowledge_base_ids": [], "stream": True},
    )
    assert response.status_code == 422
    assert response.headers["content-type"].startswith("application/json")
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_invalid_body_returns_validation_error():
    token = issue_token("user-a", settings=get_settings())
    response = _client().post(
        "/api/v1/search",
        headers={"Authorization": f"Bearer {token}"},
        json={"query": "x", "knowledge_base_ids": [str(uuid.uuid4())], "mode": "unknown"},
    )
    assert response.status_code == 422
    assert response.json()["error"]["code"] == "VALIDATION_ERROR"


def test_conversation_history_routes_are_published():
    paths = _client().get("/openapi.json").json()["paths"]
    assert "get" in paths["/api/v1/conversations"]
    assert "post" in paths["/api/v1/conversations"]
    assert "get" in paths["/api/v1/conversations/{conversation_id}"]
    assert "get" in paths["/api/v1/documents/{document_id}/docx"]
