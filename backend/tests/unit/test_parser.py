from __future__ import annotations

from knowledge.adapters.parser import (
    APPLICATION_PDF,
    DOCX,
    TEXT_CSV,
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


def _xlsx_bytes(rows, sheet_rows=None):
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    active = book.active
    active.title = "资产清单"
    for row in rows:
        active.append(row)
    if sheet_rows is not None:
        sheet = book.create_sheet("费用台账")
        for row in sheet_rows:
            sheet.append(row)
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_xlsx_is_detected_and_split_by_sheet_rows():
    from knowledge.adapters.parser import XLSX, build_parser_registry, detect_media_type

    data = _xlsx_bytes(
        [["编号", "名称"], ["ZC-001", "投影仪"], [], ["ZC-002", "白板"]],
        [["月份", "金额"], ["2026-01", 3200]],
    )
    media = detect_media_type(data, "资产.xlsx")
    assert media == XLSX

    blocks = build_parser_registry().for_file(media, "资产.xlsx").parse(data, media, "资产.xlsx")

    assert [block.locator.sheet for block in blocks] == ["资产清单", "费用台账"]
    first = blocks[0]
    # 空行跳过，但行号仍按真实 Excel 行给出。
    assert (first.locator.row_start, first.locator.row_end) == (1, 4)
    assert first.locator.type == "xlsx"
    assert first.locator.precision == "sheet"
    assert "编号\t名称" in first.text
    assert "ZC-002\t白板" in first.text
    assert "工作表《资产清单》" in first.text
    # 整数型浮点不写成 3200.0
    assert "3200" in blocks[1].text and "3200.0" not in blocks[1].text


def test_xlsx_blocks_respect_rows_per_block():
    from knowledge.adapters.parser import XLSX, XlsxParser

    rows = [["序号", "值"]] + [[str(index), str(index * 2)] for index in range(1, 96)]
    parser = XlsxParser(max_rows=5000, rows_per_block=40)
    blocks = parser.parse(_xlsx_bytes(rows), XLSX, "多行.xlsx")

    # 96 行数据按每块 40 行 => 40 + 40 + 16
    assert [len(block.text.splitlines()) - 1 for block in blocks] == [40, 40, 16]
    assert blocks[0].locator.row_start == 1
    assert blocks[-1].locator.row_end == 96


def test_xlsx_reports_truncation_instead_of_silently_dropping():
    from knowledge.adapters.parser import XLSX, XlsxParser

    rows = [["序号"]] + [[str(index)] for index in range(1, 21)]
    blocks = XlsxParser(max_rows=5, rows_per_block=5).parse(_xlsx_bytes(rows), XLSX, "长表.xlsx")

    assert len(blocks) == 1
    assert "xlsx_rows_truncated" in blocks[-1].warnings


def test_xlsx_without_content_is_a_clear_failure():
    from knowledge.adapters.parser import XLSX, XlsxParser
    from knowledge.domain.errors import ParseFailed

    try:
        XlsxParser().parse(_xlsx_bytes([]), XLSX, "空表.xlsx")
    except ParseFailed:
        return
    raise AssertionError("空表格应当给出明确失败，而不是产出空块")


def test_csv_is_detected_and_keeps_real_row_numbers():
    content = "部门,负责人\n网络部,陈建工\n\n行政部,李静\n".encode("gb18030")
    assert detect_media_type(content, "名单.csv") == TEXT_CSV

    blocks = build_parser_registry().for_file(TEXT_CSV, "名单.csv").parse(
        content, TEXT_CSV, "名单.csv"
    )

    assert len(blocks) == 1
    locator = blocks[0].locator
    assert locator.type == "csv"
    assert locator.sheet == "CSV"
    assert (locator.row_start, locator.row_end) == (1, 4)
    assert locator.precision == "sheet"
    assert "部门\t负责人" in blocks[0].text
    assert "行政部\t李静" in blocks[0].text


def _pptx_bytes(slides: list[list[str]]) -> bytes:
    import zipfile
    from io import BytesIO

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        for index, paragraphs in enumerate(slides, start=1):
            body = "".join(
                f"<a:p><a:r><a:t>{text}</a:t></a:r></a:p>" for text in paragraphs
            )
            xml = (
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
                '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
                'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
                f"<p:cSld><p:spTree>{body}</p:spTree></p:cSld></p:sld>"
            )
            archive.writestr(f"ppt/slides/slide{index}.xml", xml)
    return buffer.getvalue()


def test_pptx_extracts_slide_text_with_page_locators():
    from knowledge.adapters.parser import PPTX

    data = _pptx_bytes([["第一张幻灯片标题", "第一条内容"], ["第二张幻灯片"]])
    assert detect_media_type(data, "汇报.pptx") == PPTX

    blocks = build_parser_registry().for_file(PPTX, "汇报.pptx").parse(data, PPTX, "汇报.pptx")

    assert [block.locator.page for block in blocks] == [1, 2]
    assert blocks[0].locator.type == "pptx"
    assert "第一张幻灯片标题" in blocks[0].text
    assert "第一条内容" in blocks[0].text
    assert blocks[1].text == "第二张幻灯片"


def test_pptx_without_slides_is_a_clear_failure():
    import zipfile
    from io import BytesIO

    from knowledge.adapters.parser import PPTX, PptxParser
    from knowledge.domain.errors import ParseFailed

    buffer = BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("ppt/presentation.xml", "<presentation/>")
    try:
        PptxParser().parse(buffer.getvalue(), PPTX, "空.pptx")
    except ParseFailed:
        return
    raise AssertionError("没有幻灯片时应当给出明确失败")
