"""原文预览用的格式转换：位图转 PNG、表格转二维数据。

只做「浏览器看不懂 → 浏览器看得懂」的转换，不改动原件；原件仍由 `/file` 原样提供。
"""

from __future__ import annotations

import csv
import io
import zipfile
from typing import Any

from openpyxl import load_workbook
from PIL import Image, UnidentifiedImageError

from knowledge.adapters.parser import TEXT_CSV, XLSX, cell_text, decode_table_text
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


DOCX_MAX_ENTRIES = 2048
DOCX_MAX_UNCOMPRESSED = 100 * 1024 * 1024
DOCX_MAX_BLOCKS = 2000
DOCX_TABLE_MAX_ROWS = 200
DOCX_TABLE_MAX_COLS = 60


def docx_preview(data: bytes) -> dict[str, Any]:
    """DOCX 结构化预览：段落与表格按正文顺序返回，不保留字体、分页与图片。"""
    import docx
    from docx.oxml.ns import qn
    from docx.table import Table
    from docx.text.paragraph import Paragraph

    try:
        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > DOCX_MAX_ENTRIES
                or sum(entry.file_size for entry in entries) > DOCX_MAX_UNCOMPRESSED
            ):
                raise ParseFailed("DOCX 解压资源超限")
    except zipfile.BadZipFile as exc:
        raise ParseFailed("文档无法读取或已加密") from exc

    try:
        document = docx.Document(io.BytesIO(data))
        blocks: list[dict[str, Any]] = []
        truncated = False
        for child in document.element.body.iterchildren():
            if len(blocks) >= DOCX_MAX_BLOCKS:
                truncated = True
                break
            if child.tag == qn("w:p"):
                paragraph = Paragraph(child, document)
                text = paragraph.text.strip()
                if not text:
                    continue
                style = paragraph.style.name if paragraph.style is not None else ""
                blocks.append(
                    {
                        "type": "paragraph",
                        "text": text,
                        "heading": (style or "").lower().startswith(("heading", "标题")),
                    }
                )
            elif child.tag == qn("w:tbl"):
                table = Table(child, document)
                rows: list[list[str]] = []
                for index, row in enumerate(table.rows):
                    if index >= DOCX_TABLE_MAX_ROWS:
                        truncated = True
                        break
                    cells = [
                        cell.text.strip()
                        for cell in list(row.cells)[:DOCX_TABLE_MAX_COLS]
                    ]
                    while cells and not cells[-1]:
                        cells.pop()
                    if any(cells):
                        rows.append(cells)
                if rows:
                    blocks.append({"type": "table", "rows": rows})
        return {"kind": "docx", "blocks": blocks, "truncated": truncated}
    except ParseFailed:
        raise
    except Exception as exc:
        raise ParseFailed("文档无法读取或已加密") from exc


def sheet_preview(
    data: bytes,
    media_type: str,
    *,
    max_rows: int = SHEET_MAX_ROWS,
    max_cols: int = SHEET_MAX_COLS,
) -> dict[str, Any]:
    """把工作簿读成「表名 + 二维单元格」，供前端画成只读表格。"""
    if media_type == TEXT_CSV:
        text = decode_table_text(data)
        if text is None:
            raise ParseFailed("表格文本编码无法识别")
        csv_rows: list[list[str]] = []
        truncated = False
        for index, row in enumerate(csv.reader(io.StringIO(text))):
            if index >= max_rows:
                truncated = True
                break
            cells = [str(cell).strip() for cell in row[:max_cols]]
            while cells and not cells[-1]:
                cells.pop()
            if cells:
                csv_rows.append(cells)
        return {
            "kind": "sheet",
            "sheets": [{"name": "CSV", "rows": csv_rows, "truncated": truncated}],
            "truncated": truncated,
            "limits": {"rows": max_rows, "cols": max_cols},
        }
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
