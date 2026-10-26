"""静态页面不签发身份；业务接口仍要求原有开发令牌。"""

import io

import httpx
from fastapi.testclient import TestClient

from knowledge.entrypoints.api import create_app
from scripts.ask_console import run_console


def test_workbench_assets_and_unchanged_authentication():
    with TestClient(create_app()) as client:
        response = client.get("/workbench/")
        assert response.status_code == 200
        assert "知页" in response.text
        for file in ("style.css", "app.mjs", "core.mjs"):
            assert client.get("/workbench/" + file).status_code == 200
        assert client.get("/api/v1/knowledge-bases").status_code == 401
        assert client.get("/").json()["data"]["service"] == "knowledge-service"


def test_dev_token_display_is_explicit_and_not_an_api_request():
    def forbidden(_):
        raise AssertionError("显示本地令牌不应调用问答接口")

    output = io.StringIO()
    answers = iter(["/token", "/quit"])
    with httpx.Client(
        transport=httpx.MockTransport(forbidden),
        headers={"Authorization": "Bearer synthetic-token"},
    ) as client:
        run_console(client, "kb", read=lambda _: next(answers), output=output)
    assert "synthetic-token" in output.getvalue()
