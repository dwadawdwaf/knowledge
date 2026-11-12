"""本机MinerU协议及安全边界；仅合成响应，不证明真实解析效果。"""

import io
import json
import urllib.error
import zipfile
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from knowledge.adapters.mineru import PROFILE, MinerUParser, normalize_archive
from knowledge.adapters.parser import (
    DOC,
    DOCX,
    DirectTextParser,
    build_parser_registry,
    detect_media_type,
)
from knowledge.adapters.ragflow_text import search_fields
from knowledge.config import Settings
from knowledge.domain.errors import ParseFailed, UnsupportedFormat
from knowledge.domain.ragflow_chunking import chunk_ragflow


def test_missing_service_is_not_reported_as_document_damage():
    with (
        patch("knowledge.adapters.mineru._open", side_effect=urllib.error.URLError("SECRET")),
        pytest.raises(ParseFailed, match="服务未就绪") as error,
    ):
        MinerUParser(Settings()).parse(b"synthetic", "application/pdf", "private.pdf")
    assert "SECRET" not in str(error.value) and "private" not in str(error.value)


def archive(*, mutate=None, duplicate=False, middle=None, assets=()):
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
        if middle is not None:
            target.writestr("middle_json.json", json.dumps(middle))
        if duplicate:
            target.writestr("structured_content.json", "{}")
        for name, payload in assets:
            target.writestr(name, payload)
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


def test_ragflow_mineru_rows_preserve_hierarchical_headers_and_source_ranges():
    html = (
        "<table><caption>工况记录</caption><thead>"
        '<tr><th rowspan="2">设备</th><th colspan="2">工况</th></tr>'
        "<tr><th>温度</th><th>压力</th></tr></thead><tbody>"
        '<tr><td rowspan="2">RX-001</td><td>145<br>摄氏度</td><td>5</td></tr>'
        "<tr><td>130</td><td>4</td></tr></tbody></table>"
    )

    def mutate(content):
        content["pages"] = [{"page_idx": 2, "blocks": [{"type": "table", "content": html}]}]

    parsed = normalize_archive(archive(mutate=mutate), "application/pdf", 100000)
    chunks = chunk_ragflow(parsed, len, mineru_pdf=True)
    assert [chunk.text for chunk in chunks] == [
        "工况记录", "设备\t工况", "温度\t压力", "RX-001\t145\n摄氏度\t5", "130\t4",
    ]
    assert all(chunk.structure["table_header"] == "" for chunk in chunks[:3])
    assert chunks[3].structure["table_header"] == "设备\t工况-温度\t工况-压力"
    assert chunks[3].structure["table_fields"] == ["设备: RX-001", "工况-温度: 145 摄氏度", "工况-压力: 5"]
    assert chunks[4].structure["table_fields"] == ["工况-温度: 130", "工况-压力: 4"]
    for chunk in chunks:
        locator = chunk.locator["sources"][0]["locator"]
        assert locator["page"] == 3
        assert parsed[0].text[locator["char_start"]:locator["char_end"]] == chunk.text
    terms = search_fields(
        chunks[3].text, "巡检", [], chunks[3].structure["table_header"],
        row_fields=chunks[3].structure["table_fields"],
    )
    assert "pair:温度 145" in terms.terms


def test_ragflow_malformed_mineru_table_preserves_text_without_pairing():
    def mutate(content):
        content["pages"][0]["blocks"][1]["content"] = (
            '<table><tr><th colspan="999">设备</th></tr><tr><td>RX-001</td></tr></table>'
        )

    parsed = normalize_archive(archive(mutate=mutate), "application/pdf", 100000)
    chunks = chunk_ragflow(parsed, len, mineru_pdf=True)
    table = next(chunk for chunk in chunks if chunk.structure["kind"] == "table")
    assert table.text == parsed[1].text
    assert not table.structure["table_fields"]
    assert "mineru_table_structure_unavailable" in table.warnings


@pytest.mark.parametrize("variant", ["valid", "wrong_type", "missing_box", "wrong_version", "over_budget"])
def test_pdf_geometry_is_optional_and_tied_to_the_structured_page(variant):
    middle = {
        "schema": "docvortex.middle", "schema_version": "2.0", "is_full_document": True,
        "metadata": {"producer": {"name": "mineru", "version": "4.1.0"}},
        "extensions": {"mineru": {"parse_mode": "ocr"}},
        "pages": [{"page_idx": 0, "blocks": [
            {"type": "figure" if variant == "wrong_type" else "text",
             "bbox": [0.1, 0.2, 0.45, 0.4] if variant != "missing_box" else [0.7, 0.2, 0.45, 0.4]},
            {"type": "table", "bbox": [0.5, 0.2, 0.9, 0.4]},
        ]}],
    }
    if variant == "wrong_version":
        middle["metadata"]["producer"]["version"] = "4.1.1"
    data = archive(middle=middle)
    limit = 100000
    if variant == "over_budget":
        with zipfile.ZipFile(io.BytesIO(data)) as target:
            limit = target.getinfo("structured_content.json").file_size
    blocks = normalize_archive(data, "application/pdf", limit)
    assert blocks[0].text == "合成设备保修18个月"
    assert blocks[0].metadata["pdf_bbox"] == ((0.1, 0.2, 0.45, 0.4) if variant == "valid" else None)
    assert blocks[0].locator.positions == ()
    assert blocks[0].metadata["pdf_ocr"] is (variant not in {"wrong_version", "over_budget"})


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


def test_large_unused_members_do_not_count_against_json_budget():
    stream = io.BytesIO()
    with zipfile.ZipFile(stream, "w") as target:
        target.writestr("structured_content.json", json.dumps({
            "metadata": {"producer": {"name": "mineru", "version": "4.1.0"}},
            "is_full_document": True,
            "pages": [{"page_idx": 0, "blocks": [{"type": "text", "content": "合成正文"}]}],
        }))
        target.writestr("images/page-1.png", b"\x00" * 200000)
    result = normalize_archive(stream.getvalue(), "application/pdf", 100000)
    assert result[0].text == "合成正文"


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
    model = MinerUParser(Settings(parse_output_bytes=8, mineru_output_bytes=8))
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


def test_all_native_document_routes_are_explicit():
    profile = PROFILE
    registry = build_parser_registry(Settings(document_parser_profile="legacy-v1"))
    for mime in (
        "application/pdf",
        "image/png",
        "text/csv",
        DOC,
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
    ):
        assert registry.for_file(mime, "input", profile=profile).name == "mineru"
    for mime in ("text/plain", "text/markdown"):
        assert registry.for_file(mime, "input", profile=profile).name == "direct-text"
    assert (
        "parser_direct_text"
        in DirectTextParser().parse(b"synthetic", "text/plain", "a.txt")[0].warnings
    )


def test_legacy_word_requires_ole_signature_and_mineru():
    signature = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"
    header = signature + bytes(18) + b"\x03\x00\xfe\xff\x09\x00"
    assert detect_media_type(header.ljust(1024, b"\x00"), "word.DOC") == DOC
    assert detect_media_type(header, "word.doc") != DOC
    assert detect_media_type(b"not-ole", "word.doc") != DOC
    with pytest.raises(UnsupportedFormat):
        build_parser_registry(Settings()).for_file(DOC, "word.doc", profile="legacy-v1")


@pytest.mark.parametrize("media", [DOC, DOCX])
def test_word_image_only_is_searchable_text_with_word_locator(media):
    payload = b"\x89PNG\r\n\x1a\nsynthetic"
    image = {"type": "image", "image_source": "images/page_0_image_1.png", "content": ""}
    word_result = archive(
        mutate=lambda p: p["pages"][0].update(blocks=[image]),
        assets=[(image["image_source"], payload)],
    )
    with patch("knowledge.adapters.mineru._open", side_effect=[
        io.BytesIO(word_result), io.BytesIO(archive()),
    ]) as opened:
        result = MinerUParser(Settings()).parse(b"fixture", media, "private.docx")
    assert result[0].text == "合成设备保修18个月"
    assert result[0].locator.type == ("doc" if media == DOC else "docx")
    assert result[0].locator.page is None and result[0].locator.precision == "block"
    assert "word_image_ocr_requires_review" in result[0].warnings
    body = opened.call_args_list[1].args[0].data
    assert b'name="ocr_mode"\r\n\r\nocr' in body and payload in body
    assert b"private.docx" not in body


def test_word_images_in_tables_are_local_bounded_and_empty_ocr_is_warned():
    png = b"\x89PNG\r\n\x1a\nsynthetic"
    table = {"type": "table", "content": (
        '<table><tr><td>甲<img src="images/safe.png"><img src="https://untrusted/remote.png">'
        '<img src="images/large.png"></td></tr></table>'
    )}
    word_result = archive(mutate=lambda p: p["pages"][0].update(blocks=[table]), assets=[
        ("images/safe.png", png), ("images/large.png", png * 2),
    ])
    with (
        patch("knowledge.adapters.mineru._open", side_effect=[
            io.BytesIO(word_result), io.BytesIO(archive(mutate=lambda p: p["pages"][0].update(blocks=[]))),
        ]) as opened,
        patch("knowledge.adapters.mineru._MAX_OFFICE_IMAGES", 1),
    ):
        result = MinerUParser(Settings()).parse(b"fixture", DOCX, "fixture.docx")
    assert opened.call_count == 2
    assert result[0].text == "甲"
    assert {"word_image_ocr_asset_unavailable", "word_image_ocr_budget_exceeded", "word_image_ocr_no_text"} <= set(
        result[0].warnings
    )


def test_word_ocr_groups_by_image_occurrence_preserving_order_and_offsets():
    name = "images/page_0_image_1.png"
    image = {"type": "image", "image_source": name, "content": ""}
    question, answer = "设备检查频率是多少？", "每周检查一次。"
    word_result = archive(
        mutate=lambda p: p["pages"][0].update(blocks=[
            {"type": "text", "content": "图片前的 Word 正文"}, image, image,
            {"type": "text", "content": "图片后的 Word 正文"},
        ]),
        assets=[(name, b"\x89PNG\r\n\x1a\nsynthetic")],
    )
    ocr = archive(mutate=lambda p: p["pages"][0].update(blocks=[
        {"type": "paragraph_title", "content": question}, {"type": "text", "content": answer},
    ]))
    with patch("knowledge.adapters.mineru._open", side_effect=[
        io.BytesIO(word_result), io.BytesIO(ocr), io.BytesIO(ocr),
    ]):
        blocks = MinerUParser(Settings()).parse(b"fixture", DOCX, "synthetic.docx")

    chunks = chunk_ragflow(blocks, len)
    assert [part.text for part in chunks] == [
        "图片前的 Word 正文", f"{question}\n{answer}", f"{question}\n{answer}", "图片后的 Word 正文",
    ]
    for part, expected in zip(chunks[1:3], ([1, 2], [3, 4]), strict=True):
        assert part.locator["precision"] == "composite" and "page" not in part.locator
        assert "word_image_ocr_requires_review" in part.warnings
        assert [item["locator"]["block"] for item in part.locator["sources"]] == expected
        for source in part.locator["sources"]:
            loc = source["locator"]
            assert part.text[source["chunk_start"]:source["chunk_end"]] == blocks[loc["block"]].text[
                loc["char_start"]:loc["char_end"]
            ]

    # 按当前 RAGFlow token 预算强制拆分；两个图片位置仍分别保留来源。
    limited = chunk_ragflow(blocks, lambda text: len(text) * 40)
    assert [part.text for part in limited[1:-1]] == [question, answer, question, answer]
    assert [part.locator["block"] for part in limited[1:-1]] == [1, 2, 3, 4]
