"""预览转换：表格转二维数据、位图转 PNG。"""

from __future__ import annotations

import pathlib

import pytest

from knowledge.adapters.parser import XLSX
from knowledge.adapters.preview import sheet_preview, to_png
from knowledge.domain.errors import ParseFailed, UnsupportedFormat

TESTDATA = pathlib.Path(__file__).resolve().parents[2] / "testdata" / "multiformat"
SHEET_FILE = TESTDATA / "表格-资产与费用台账.xlsx"
TIFF_FILE = TESTDATA / "扫描件-图书角借阅规则.tiff"


def test_sheet_preview_returns_sheets_and_rows() -> None:
    result = sheet_preview(SHEET_FILE.read_bytes(), XLSX)
    assert result["kind"] == "sheet"
    names = [sheet["name"] for sheet in result["sheets"]]
    assert names == ["资产清单", "费用台账"]
    first = result["sheets"][0]
    assert first["rows"][0][:3] == ["编号", "名称", "类别"]
    assert result["limits"]["rows"] > 0


def test_sheet_preview_truncates_by_row_limit() -> None:
    result = sheet_preview(SHEET_FILE.read_bytes(), XLSX, max_rows=3)
    assert result["truncated"] is True
    assert all(len(sheet["rows"]) <= 3 for sheet in result["sheets"])


def test_sheet_preview_rejects_other_media_types() -> None:
    with pytest.raises(UnsupportedFormat):
        sheet_preview(b"not a sheet", "text/markdown")


def test_to_png_converts_tiff_to_png_header() -> None:
    png = to_png(TIFF_FILE.read_bytes())
    assert png.startswith(b"\x89PNG\r\n\x1a\n")
    assert len(png) > 1024


def test_to_png_rejects_broken_image() -> None:
    with pytest.raises(ParseFailed):
        to_png(b"not an image")
