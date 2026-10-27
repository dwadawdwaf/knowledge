"""DeepDoc 协议与版本路由测试；不冒充真实 OCR 效果验收。"""

from __future__ import annotations

import json
from io import BytesIO
from unittest.mock import patch

import pytest

from knowledge.adapters.deepdoc import DeepDocParser
from knowledge.adapters.parser import APPLICATION_PDF, build_parser_registry, detect_media_type
from knowledge.config import Settings
from knowledge.domain.errors import ParseFailed, UnsupportedFormat


def payload() -> dict:
    return {
        "profile": "deepdoc-v0.22.0-v1",
        "warnings": ["deepdoc_ocr_requires_review", "deepdoc_table_linearized"],
        "blocks": [
            {"text": "部门\t额度\n行政\t100", "page": 2, "positions": [[2, 10, 100, 20, 80]]}
        ],
    }


def parser() -> DeepDocParser:
    return DeepDocParser(Settings(deepdoc_url="http://127.0.0.1:9381", deepdoc_token="test"))


def test_deepdoc_preserves_location_and_warnings():
    response = BytesIO(json.dumps(payload()).encode())
    with patch("knowledge.adapters.deepdoc._open", return_value=response) as opened:
        blocks = parser().parse(b"%PDF-1.7", APPLICATION_PDF, "private.pdf")
    assert blocks[0].locator.page == 2
    assert blocks[0].locator.positions == ((2, 10.0, 100.0, 20.0, 80.0),)
    assert blocks[0].locator.char_end == len(blocks[0].text)
    assert "deepdoc_table_linearized" in blocks[0].warnings
    request = opened.call_args.args[0]
    assert request.data == b"%PDF-1.7"
    assert "private.pdf" not in str(request.headers)


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(profile="wrong"),
        lambda p: p.update(warnings=["secret internal path"]),
        lambda p: p["blocks"][0].update(page=0),
        lambda p: p["blocks"][0].update(positions=[[2, 100, 10, 20, 80]]),
        lambda p: p["blocks"][0].update(positions=[[3, 10, 100, 20, 80]]),
        lambda p: p["blocks"][0].update(positions=[[2, 10, float("nan"), 20, 80]]),
        lambda p: p["blocks"][0].update(text=123),
        lambda p: p.update(blocks=[]),
    ],
)
def test_deepdoc_rejects_invalid_response(mutate):
    data = payload()
    mutate(data)
    with (
        patch("knowledge.adapters.deepdoc._open", return_value=BytesIO(json.dumps(data).encode())),
        pytest.raises(ParseFailed),
    ):
        parser().parse(b"pdf", APPLICATION_PDF, "a.pdf")


def test_deepdoc_does_not_fallback_on_outage():
    with (
        patch("knowledge.adapters.deepdoc._open", side_effect=OSError("secret")),
        pytest.raises(ParseFailed, match="DeepDoc") as failure,
    ):
        parser().parse(b"pdf", APPLICATION_PDF, "a.pdf")
    assert "secret" not in str(failure.value)


def test_deepdoc_response_is_bounded():
    limited = Settings(
        deepdoc_url="http://127.0.0.1:9381", deepdoc_token="test", parse_output_bytes=32
    )
    with (
        patch("knowledge.adapters.deepdoc._open", return_value=BytesIO(b" " * 33)),
        pytest.raises(ParseFailed),
    ):
        DeepDocParser(limited).parse(b"pdf", APPLICATION_PDF, "a.pdf")


def test_deepdoc_preserves_password_protected_error():
    from knowledge.domain.errors import PasswordProtected

    with (
        patch(
            "knowledge.adapters.deepdoc._open",
            return_value=BytesIO(b'{"error":"PASSWORD_PROTECTED"}'),
        ),
        pytest.raises(PasswordProtected),
    ):
        parser().parse(b"pdf", APPLICATION_PDF, "a.pdf")


def test_registry_keeps_old_builds_on_legacy_parser():
    registry = build_parser_registry(Settings(document_parser_profile="deepdoc-v0.22.0-v1"))
    assert registry.for_file(APPLICATION_PDF, "a.pdf").name == "deepdoc"
    assert registry.for_file(APPLICATION_PDF, "a.pdf", profile="legacy-v1").name == "pdf"
    assert registry.for_file("image/png", "a.png").name == "deepdoc"
    with pytest.raises(UnsupportedFormat):
        registry.for_file("image/png", "a.png", profile="legacy-v1")
    with pytest.raises(ParseFailed):
        registry.for_file(APPLICATION_PDF, "a.pdf", profile="unknown")


@pytest.mark.parametrize(
    "data,media",
    [
        (b"\x89PNG\r\n\x1a\nrest", "image/png"),
        (b"\xff\xd8\xffrest", "image/jpeg"),
        (b"II*\x00rest", "image/tiff"),
        (b"MM\x00*rest", "image/tiff"),
    ],
)
def test_image_signature_not_extension(data, media):
    assert detect_media_type(data, "disguised.txt") == media
    assert detect_media_type(b"not an image", "fake.png") == "application/octet-stream"


@pytest.mark.parametrize(
    "url",
    ["https://remote.example", "http://user:pass@localhost:9381", "http://localhost:9381/path"],
)
def test_parser_requires_loopback_origin(url):
    with pytest.raises(ValueError):
        DeepDocParser(Settings(deepdoc_url=url))
