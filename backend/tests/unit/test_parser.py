from __future__ import annotations

from knowledge.adapters.parser import (
    APPLICATION_PDF,
    DOCX,
    TEXT_MARKDOWN,
    TEXT_PLAIN,
    build_parser_registry,
    detect_media_type,
)
from knowledge.domain.errors import UnsupportedFormat


def test_detect_by_extension_for_text():
    assert detect_media_type("正文".encode(), "制度.md") == TEXT_MARKDOWN
    assert detect_media_type("正文".encode(), "制度.txt") == TEXT_PLAIN


def test_detect_by_signature_overrides_extension():
    assert detect_media_type(b"%PDF-1.7 ...", "伪装.md") == APPLICATION_PDF
    assert detect_media_type(b"PK\x03\x04rest", "报告.docx") == DOCX


def test_unknown_binary_not_text():
    assert detect_media_type(b"\xff\xfe\x00\x00", "x.txt") == "application/octet-stream"


def test_text_parser_blocks_keep_offsets():
    content = "第一段规定。\n\n第二段规定。\n".encode()
    blocks = build_parser_registry().for_file(TEXT_PLAIN, "a.txt").parse(
        content, TEXT_PLAIN, "a.txt"
    )
    assert len(blocks) == 2
    assert blocks[0].locator.char_start == 0
    assert blocks[1].locator.char_start > blocks[0].locator.char_end


def test_registry_rejects_unknown_format():
    registry = build_parser_registry()
    try:
        registry.for_file("image/png", "x.png")
    except UnsupportedFormat:
        return
    raise AssertionError("应当拒绝未知格式")
