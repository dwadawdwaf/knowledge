"""原文预览用的格式转换：位图转 PNG、表格转二维数据。

只做「浏览器看不懂 → 浏览器看得懂」的转换，不改动原件；原件仍由 `/file` 原样提供。
"""

from __future__ import annotations

import io
from typing import Any

from openpyxl import load_workbook
from PIL import Image, UnidentifiedImageError

from knowledge.adapters.parser import XLSX, cell_text
from knowledge.domain.errors import ParseFailed, UnsupportedFormat

# 预览图最长边；超过就让浏览器缩放，避免把几十兆的扫描件塞给前端
MAX_IMAGE_SIDE = 2400
# 表格预览的截断上限：够看清结构，又不会把整个表拉进内存
SHEET_MAX_ROWS = 200
SHEET_MAX_COLS = 60


def to_png(data: bytes, *, max_side: int = MAX_IMAGE_SIDE) -> bytes:
    """把浏览器不支持的位图（如 TIFF）转成 PNG；多页 TIFF 只取第一页。"""
    try:
        with Image.open(io.BytesIO(data)) as image:
            image.seek(0)
            frame = image.copy()
            if frame.mode not in {"RGB", "RGBA", "L"}:
                frame = frame.convert("RGB")
            frame.thumbnail((max_side, max_side), Image.Resampling.LANCZOS)
            buffer = io.BytesIO()
            frame.save(buffer, format="PNG")
            return buffer.getvalue()
    except (UnidentifiedImageError, OSError) as exc:
        raise ParseFailed("图片无法转换成可预览格式") from exc


def sheet_preview(
    data: bytes,
    media_type: str,
    *,
    max_rows: int = SHEET_MAX_ROWS,
    max_cols: int = SHEET_MAX_COLS,
) -> dict[str, Any]:
    """把工作簿读成「表名 + 二维单元格」，供前端画成只读表格。"""
    if media_type != XLSX:
        raise UnsupportedFormat("只有表格文件支持表格预览")

    try:
        workbook = load_workbook(io.BytesIO(data), read_only=True, data_only=True)
    except Exception as exc:  # 统一转为可预期的解析失败
        raise ParseFailed("表格无法读取或已加密") from exc

    sheets: list[dict[str, Any]] = []
    truncated_any = False
    try:
        for sheet in workbook.worksheets:
            rows: list[list[str]] = []
            truncated = False
            for index, row in enumerate(sheet.iter_rows(values_only=True)):
                if index >= max_rows:
                    truncated = True
                    break
                cells = [cell_text(value) for value in row[:max_cols]]
                while cells and not cells[-1]:
                    cells.pop()
                if cells:
                    rows.append(cells)
            sheets.append(
                {"name": sheet.title or "Sheet", "rows": rows, "truncated": truncated}
            )
            truncated_any = truncated_any or truncated
    finally:
        workbook.close()

    return {
        "kind": "sheet",
        "sheets": sheets,
        "truncated": truncated_any,
        "limits": {"rows": max_rows, "cols": max_cols},
    }
