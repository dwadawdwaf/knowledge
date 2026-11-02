"""本机MinerU协议及安全边界；仅合成响应，不证明真实解析效果。"""

import io
import json
import urllib.error
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from knowledge.adapters.mineru import PROFILE, MinerUParser, normalize_archive
from knowledge.adapters.parser import DirectTextParser, build_parser_registry
from knowledge.config import Settings
from knowledge.domain.errors import ParseFailed


def test_missing_service_is_not_reported_as_document_damage():
    with (
        patch("knowledge.adapters.mineru._open", side_effect=urllib.error.URLError("SECRET")),
        pytest.raises(ParseFailed, match="服务未就绪") as error,
    ):
        MinerUParser(Settings()).parse(b"synthetic", "application/pdf", "private.pdf")
    assert "SECRET" not in str(error.value) and "private" not in str(error.value)


def archive(*, mutate=None, duplicate=False):
    content = {
        "metadata": {"producer": {"name": "mineru", "version": "4.1.0"}},
        "is_full_document": True,
        "pages": [
            {
                "page_idx": 0,
                "blocks": [
                    {"type": "text", "content": "合成设备保修18个月"},
                    {"type": "table", "content": "<table><tr><td>甲</td><td>18</td></tr></table>"},
                ],
            }
        ],
    }
    if mutate:
        mutate(content)
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        target.writestr("structured_content.json", json.dumps(content))
        if duplicate:
            target.writestr("structured_content.json", "{}")
        target.writestr("../../must-not-be-written.txt", "not extracted")
    return stream.getvalue()


def test_archive_keeps_source_pages_and_explicit_loss_of_geometry():
    result = normalize_archive(archive(), "application/pdf", 100000)
    assert result[0].locator.page == 1
    assert result[0].locator.precision == "page" and result[0].locator.positions == ()
    assert result[1].text == "甲\t18"
    assert "mineru_table_linearized" in result[1].warnings
    csv = normalize_archive(archive(), "text/csv", 100000)
    assert csv[0].locator.type == "csv" and csv[0].locator.page is None
    assert "mineru_native_location_unavailable" in csv[0].warnings


@pytest.mark.parametrize(
    "mutate",
    [
        lambda p: p.update(is_full_document=False),
        lambda p: p["metadata"]["producer"].update(version="3.9.0"),
        lambda p: p["pages"][0].update(page_idx=True),
        lambda p: p["pages"][0].update(page_idx=-1),
        lambda p: p.update(pages=[]),
        lambda p: p["pages"].append(p["pages"][0]),
    ],
)
def test_bad_or_partial_output_is_not_published(mutate):
    with pytest.raises(ValueError):
        normalize_archive(archive(mutate=mutate), "application/pdf", 100000)


def test_duplicate_member_and_decompression_budget_are_rejected():
    with pytest.warns(UserWarning), pytest.raises(ValueError):
        normalize_archive(archive(duplicate=True), "application/pdf", 100000)
    with pytest.raises(ValueError):
        normalize_archive(archive(), "application/pdf", 2)


def test_nontext_is_explicitly_skipped_and_html_does_not_execute():
    def change(p):
        p["pages"][0]["blocks"].append({"type": "image", "content": {"url": "https://untrusted"}})
        p["pages"][0]["blocks"][1]["content"] = "<script>SECRET</script><p>合成</p>"

    result = normalize_archive(archive(mutate=change), "application/pdf", 100000)
    assert "mineru_content_skipped" in result[0].warnings
    assert "SECRET" not in result[1].text


def test_plain_comparison_is_not_mistaken_for_html():
    def change(p):
        p["pages"][0]["blocks"] = [{"type": "text", "content": "x<y 且 y>0"}]

    result = normalize_archive(archive(mutate=change), "application/pdf", 100000)
    assert result[0].text == "x<y 且 y>0"
    assert "mineru_table_linearized" not in result[0].warnings


@pytest.mark.parametrize("kind,value", [("text", "甲\x00乙"), ("table", "<p>甲\x00乙</p>")])
def test_nul_replacement_preserves_offsets_and_marks_loss(kind, value):
    def change(p):
        p["pages"][0]["blocks"] = [{"type": kind, "content": value}]

    result = normalize_archive(archive(mutate=change), "application/pdf", 100000)
    assert result[0].text == "甲\ufffd乙"
    assert result[0].locator.char_end == 3
    assert "mineru_nul_replaced" in result[0].warnings
    assert (
        "mineru_nul_replaced"
        not in normalize_archive(archive(), "application/pdf", 100000)[0].warnings
    )


@pytest.mark.parametrize(
    "url",
    [
        "https://127.0.0.1",
        "http://external.example",
        "http://127.0.0.1/path",
        "http://user:pass@localhost",
        "http://localhost?key=bad",
    ],
)
def test_mineru_endpoint_must_be_loopback_origin(url):
    with pytest.raises(ValueError):
        MinerUParser(Settings(mineru_url=url))


def test_multipart_poll_and_download_never_follow_returned_url():
    model = MinerUParser(Settings(mineru_token="synthetic-token"))
    status = {
        "request_id": "synthetic",
        "status": "running",
        "status_url": "http://external.example",
        "result_url": "http://external.example",
    }
    with (
        patch(
            "knowledge.adapters.mineru.uuid.uuid4", return_value=SimpleNamespace(hex="synthetic")
        ),
        patch("knowledge.adapters.mineru.time.sleep"),
        patch(
            "knowledge.adapters.mineru._open",
            side_effect=[
                io.BytesIO(json.dumps(status).encode()),
                io.BytesIO(json.dumps({**status, "status": "completed"}).encode()),
                io.BytesIO(archive()),
            ],
        ) as opened,
    ):
        blocks = model.parse(b"%PDF-synthetic", "application/pdf", "PRIVATE.pdf")
    assert blocks[0].text == "合成设备保修18个月"
    requests = [c.args[0] for c in opened.call_args_list]
    assert [r.full_url for r in requests] == [
        "http://127.0.0.1:8003/v1/file_parse",
        "http://127.0.0.1:8003/v1/file_parse/synthetic",
        "http://127.0.0.1:8003/v1/file_parse/synthetic/result",
    ]
    assert b"PRIVATE" not in requests[0].data and b"input.pdf" in requests[0].data
    assert all(r.get_header("Authorization") == "Bearer synthetic-token" for r in requests)
    assert b'name="page_range"\r\n\r\nall' in requests[0].data


def test_mineru_outage_and_oversize_do_not_fallback_or_leak_errors():
    model = MinerUParser(Settings(parse_output_bytes=8))
    for response in (OSError("private-secret"), io.BytesIO(b"x" * 9)):
        with (
            patch(
                "knowledge.adapters.mineru._open",
                **(
                    {"side_effect": response}
                    if isinstance(response, Exception)
                    else {"return_value": response}
                ),
            ),
            pytest.raises(ParseFailed) as error,
        ):
            model.parse(b"pdf", "application/pdf", "a.pdf")
        assert "private-secret" not in str(error.value)


@pytest.mark.parametrize("profile", ["deepdoc-v0.22.0-v2", PROFILE])
def test_all_native_document_routes_are_explicit(profile):
    registry = build_parser_registry(Settings(document_parser_profile="legacy-v1"))
    for mime in (
        "application/pdf",
        "image/png",
        "text/csv",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ):
        assert registry.for_file(mime, "input", profile=profile).name == (
            "deepdoc" if profile.startswith("deepdoc") else "mineru"
        )
    for mime in ("text/plain", "text/markdown"):
        assert registry.for_file(mime, "input", profile=profile).name == "direct-text"
    assert (
        "parser_direct_text"
        in DirectTextParser().parse(b"synthetic", "text/plain", "a.txt")[0].warnings
    )
