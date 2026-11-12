"""真实 HTTP 验证 SSE 的身份、同步、状态变化和连接释放。"""

import time
from contextlib import ExitStack

import psycopg
import pytest

from knowledge.config import Settings
from knowledge.domain.errors import AuthenticationFailed
from knowledge.security import issue_token, verify_token
from scripts.http_scenario import expect


def next_event(lines):
    for line in lines:
        if line.startswith("event: "):
            event = line.removeprefix("event: ")
            assert next(lines) == 'data: {"version":1}'
            assert next(lines) == ""
            return event
    raise AssertionError("SSE 连接提前结束")


def test_stream_auth_sync_change_reconnect_and_close(runtime):
    with runtime.client() as client:
        assert client.get("/api/v1/events", headers={"Authorization": ""}).status_code == 401
        for _ in range(2):
            with client.stream("GET", "/api/v1/events") as response:
                assert response.status_code == 200
                assert response.headers["content-type"].startswith("text/event-stream")
                assert response.headers["x-accel-buffering"] == "no"
                lines = response.iter_lines()
                assert next_event(lines) == "sync"
                expect(client.post("/api/v1/knowledge-bases", json={"name": "自制实时同步"}), 201)
                assert next_event(lines) == "change"
    dsn = runtime.database_url.replace("postgresql+psycopg://", "postgresql://")
    deadline = time.monotonic() + 5
    with psycopg.connect(dsn, autocommit=True) as connection:
        while time.monotonic() < deadline:
            remaining = connection.execute(
                "SELECT count(*) FROM pg_stat_activity WHERE application_name='ks-document-events'"
            ).fetchone()[0]
            if remaining == 0:
                break
            time.sleep(0.05)
        assert remaining == 0


def test_expired_token_ends_stream(runtime):
    settings = Settings.model_construct(token_secret=runtime.token_secret)
    with runtime.client() as client:
        # 客户端准备完再签发，留出建连窗口，验证的是连接期间过期。
        token = issue_token(runtime.owner, ttl_seconds=3, settings=settings)
        with client.stream("GET", "/api/v1/events", headers={"Authorization": f"Bearer {token}"}) as response:
            assert response.status_code == 200
            lines = response.iter_lines()
            assert next_event(lines) == "sync"
            time.sleep(3.1)
            with pytest.raises(AuthenticationFailed):
                verify_token(token, settings)
            expect(client.post("/api/v1/knowledge-bases", json={"name": "自制过期验证"}), 201)
            assert next_event(lines) == "expired"
            assert list(lines) == []


def test_stream_limit_keeps_rest_available(runtime):
    with runtime.client() as client, ExitStack() as stack:
        for _ in range(8):
            response = stack.enter_context(client.stream("GET", "/api/v1/events"))
            assert response.status_code == 200
            assert next_event(response.iter_lines()) == "sync"
        response = client.get("/api/v1/events")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "EVENT_STREAM_UNAVAILABLE"
        assert client.get("/api/v1/knowledge-bases").status_code == 200
