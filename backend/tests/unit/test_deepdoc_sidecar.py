"""独立服务的协议与格式归一化测试，不加载 DeepDoc 模型。"""

from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError

from deepdoc_sidecar.runtime import normalize_boxes
from deepdoc_sidecar.service import SidecarSettings, create_app
from knowledge.domain.errors import ParseFailed

TOKEN = "test-only-" + "x" * 32
HEADERS = {
    "Authorization": "Bearer " + TOKEN,
    "X-Parser-Profile": "deepdoc-v0.22.0-v1",
    "Content-Type": "application/pdf",
}


def boxes():
    return [
        {
            "text": "<table><tr><th>部门</th><th>额度</th></tr>"
            "<tr><td>行政</td><td>100</td></tr></table>",
            "layout_type": "table",
            "positions": [[2, 10, 100, 20, 80]],
        },
        {"text": "不可描述的图", "layout_type": "figure"},
    ]


def test_table_retains_rows_and_columns_without_rendering_html():
    result = normalize_boxes(boxes(), 3)
    assert result[0].text == "部门\t额度\n行政\t100"
    assert result[0].locator.page == 2
    assert result[0].warnings == (
        "deepdoc_empty_page",
        "deepdoc_figures_not_described",
        "deepdoc_ocr_requires_review",
        "deepdoc_table_linearized",
    )


def test_missing_location_is_failure_not_fabricated_page_one():
    with pytest.raises(ParseFailed):
        normalize_boxes([{"text": "text"}], 1)


def test_sidecar_auth_profile_limits_and_sanitized_failure():
    client = TestClient(create_app(SidecarSettings(token=TOKEN, input_bytes=16)))
    with patch("deepdoc_sidecar.service.BoundedParser.parse", side_effect=RuntimeError("secret")):
        assert client.post("/v1/parse", content=b"pdf").status_code == 401
        assert (
            client.post(
                "/v1/parse", headers={**HEADERS, "X-Parser-Profile": "bad"}, content=b"pdf"
            ).status_code
            == 409
        )
        assert client.post("/v1/parse", headers=HEADERS, content=b"x" * 17).status_code == 413
        response = client.post("/v1/parse", headers=HEADERS, content=b"pdf")
        assert response.status_code == 422
        assert response.json() == {"error": "PARSE_FAILED"}
    assert client.get("/health").json()["models_verified"] is False


def test_sidecar_serializes_contract_fixture_without_model():
    client = TestClient(create_app(SidecarSettings(token=TOKEN)))
    with patch(
        "deepdoc_sidecar.service.BoundedParser.parse", return_value=normalize_boxes(boxes(), 3)
    ):
        response = client.post("/v1/parse", headers=HEADERS, content=b"pdf")
    assert response.status_code == 200
    assert response.json()["blocks"][0]["positions"] == [[2, 10, 100, 20, 80]]
    assert "deepdoc_table_linearized" in response.json()["warnings"]


def test_invalid_configuration_never_prints_token():
    with pytest.raises(ValidationError) as failure:
        SidecarSettings(token="private-short-token")
    assert "private-short-token" not in str(failure.value)


def test_warnings_attach_per_block_and_reading_order_is_sorted():
    boxes = [
        {"text": "第二条　内容乙这句话足够长不会触发短块合并。", "layout_type": "text",
         "positions": [[1, 30, 260, 45, 70]]},
        {"text": "<table><tr><td>A</td></tr></table>", "layout_type": "table",
         "positions": [[1, 30, 300, 200, 240]]},
        {"text": "第一条　内容甲这句话足够长不会触发短块合并。", "layout_type": "text",
         "positions": [[1, 30, 120, 20, 40]]},
    ]
    result = normalize_boxes(boxes, 1)
    assert [b.text[:3] for b in result] == ["第一条", "第二条", "A"]
    assert "deepdoc_table_linearized" in result[2].warnings
    # 纯文本块不得带上表格警告：只看 build 并集无法判断哪一块是表格。
    assert "deepdoc_table_linearized" not in result[0].warnings
    assert result[0].warnings == ("deepdoc_ocr_requires_review",)
    assert result[0].locator.page == 1
    # 表格定位在上边距 200，应排在两条正文之后。
    assert result[2].locator.page == 1


def test_short_heading_merges_into_following_block():
    boxes = [
        {"text": "第二条", "layout_type": "text", "positions": [[1, 30, 120, 20, 40]]},
        {"text": "出差前应提交行程单并经批准。", "layout_type": "text",
         "positions": [[1, 30, 130, 45, 70]]},
    ]
    result = normalize_boxes(boxes, 1)
    assert len(result) == 1
    assert result[0].text == "第二条 出差前应提交行程单并经批准。"
