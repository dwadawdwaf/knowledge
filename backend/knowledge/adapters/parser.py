"""解析器：原文件 → 有序内容块。不判断业务规则，只交付文本与定位。"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from dataclasses import replace
from datetime import datetime, time

from knowledge.config import Settings
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed, PasswordProtected, UnsupportedFormat
from knowledge.domain.ports import Parser

TEXT_PLAIN = "text/plain"
TEXT_MARKDOWN = "text/markdown"
TEXT_CSV = "text/csv"
APPLICATION_PDF = "application/pdf"
DOC = "application/msword"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
XLSX = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
PPTX = "application/vnd.openxmlformats-officedocument.presentationml.presentation"
OCTET_STREAM = "application/octet-stream"

_EXT_MAP = {
    ".txt": TEXT_PLAIN,
    ".md": TEXT_MARKDOWN,
    ".markdown": TEXT_MARKDOWN,
    ".csv": TEXT_CSV,
    ".pdf": APPLICATION_PDF,
    ".docx": DOCX,
    ".xlsx": XLSX,
    ".pptx": PPTX,
}
_PARAGRAPH_BREAK = re.compile(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*")


def decode_table_text(data: bytes) -> str | None:
    """表格文本优先按 UTF-8（含 BOM），失败再按 GB18030；都不行则视为二进制。"""
    for encoding in ("utf-8-sig", "gb18030"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return None


def detect_media_type(data: bytes, filename: str) -> str:
    """按内容签名判断类型，扩展名只作兜底。"""
    if data.startswith(b"%PDF"):
        return APPLICATION_PDF
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if filename.lower().endswith(".doc") and data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        # 复合文件头至少还有一个扇区；具体 WordDocument 流交给 MinerU 检验。
        header = data[26:32]
        if (header == b"\x03\x00\xfe\xff\x09\x00" and len(data) >= 1024) or (
            header == b"\x04\x00\xfe\xff\x0c\x00" and len(data) >= 8192
        ):
            return DOC
    if data.startswith(b"PK\x03\x04"):
        # 两者都是 zip 容器，只能靠扩展名区分；认不出就交给白名单兜底拒绝。
        lowered = filename.lower()
        if lowered.endswith(".docx"):
            return DOCX
        if lowered.endswith(".xlsx"):
            return XLSX
        if lowered.endswith(".pptx"):
            return PPTX

    suffix = ""
    if "." in filename:
        suffix = "." + filename.rsplit(".", 1)[-1].lower()
    declared = _EXT_MAP.get(suffix)
    if declared in (TEXT_PLAIN, TEXT_MARKDOWN):
        try:
            data.decode("utf-8")
            return declared or TEXT_PLAIN
        except UnicodeDecodeError:
            return OCTET_STREAM
    # CSV 常见 GBK/GB18030 编码，不能只按 UTF-8 判死；仍要求能解码成文本。
    if declared == TEXT_CSV:
        return TEXT_CSV if decode_table_text(data) is not None else OCTET_STREAM
    return OCTET_STREAM


def _split_paragraphs(text: str) -> list[tuple[int, int]]:
    """按 LF 或 CRLF 空行切块，保留原文偏移区间。"""
    # 不先替换换行：偏移必须仍能切回原始解码文本，包括 CRLF 中的两个码点。
    spans: list[tuple[int, int]] = []
    cursor = 0
    for separator in _PARAGRAPH_BREAK.finditer(text):
        end = separator.start()
        segment = text[cursor:end]
        trimmed = segment.lstrip(" \t\r\n")
        if trimmed.strip():
            spans.append((end - len(trimmed), end))
        cursor = separator.end()
    tail = text[cursor:].lstrip(" \t\r\n")
    if tail.strip():
        spans.append((len(text) - len(tail), len(text)))
    return spans


class TextParser:
    name = "text"

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type in (TEXT_PLAIN, TEXT_MARKDOWN)

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        text = data.decode("utf-8", errors="replace")
        blocks: list[ParsedBlock] = []
        for index, (start, end) in enumerate(_split_paragraphs(text)):
            piece = text[start:end]
            if not piece.strip():
                continue
            blocks.append(
                ParsedBlock(
                    text=piece,
                    locator=Locator(
                        type="md" if media_type == TEXT_MARKDOWN else "txt",
                        char_start=start,
                        char_end=end,
                        precision="char",
                        block=index,
                    ),
                )
            )
        return blocks


class PdfParser:
    name = "pdf"

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == APPLICATION_PDF

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PasswordProtected("PDF 已加密，无法解析")
        blocks: list[ParsedBlock] = []

        for page_index, page in enumerate(reader.pages, start=1):
            page_text = page.extract_text() or ""
            if not page_text.strip():
                continue
            for local_index, (start, end) in enumerate(_split_paragraphs(page_text)):
                piece = page_text[start:end]
                if not piece.strip():
                    continue
                blocks.append(
                    ParsedBlock(
                        text=piece,
                        locator=Locator(
                            type="pdf",
                            page=page_index,
                            block=local_index,
                            char_start=start,
                            char_end=end,
                            precision="block",
                        ),
                    )
                )
        return blocks


class DocxParser:
    name = "docx"

    def __init__(self, max_entries: int = 2048, max_uncompressed_bytes: int = 100 * 1024 * 1024):
        self.max_entries = max_entries
        self.max_uncompressed_bytes = max_uncompressed_bytes

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == DOCX

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        import docx

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > self.max_entries
                or sum(entry.file_size for entry in entries) > self.max_uncompressed_bytes
            ):
                raise ParseFailed("DOCX 解压资源超限")
            has_images = any(entry.filename.startswith("word/media/") and not entry.is_dir() for entry in entries)
        document = docx.Document(io.BytesIO(data))
        order = {element._element: index for index, element in enumerate(document.iter_inner_content())}
        blocks: list[ParsedBlock] = []
        for index, paragraph in enumerate(document.paragraphs):
            text = paragraph.text
            if not text.strip():
                continue
            blocks.append(
                ParsedBlock(
                    text=text,
                    kind="heading" if paragraph.style and paragraph.style.name.startswith("Heading ") else "text",
                    heading_level=int(paragraph.style.name.split()[-1])
                    if paragraph.style and re.fullmatch(r"Heading [1-6]", paragraph.style.name) else None,
                    metadata={"order": order.get(paragraph._element, index)},
                    locator=Locator(
                        type="docx",
                        paragraph=index,
                        char_start=0,
                        char_end=len(text),
                        precision="block",
                    ),
                )
            )
        # 表格按行展平：结构化表格解析不在首期范围（AGENTS §3.6），
        # 但单元格内容必须参与索引，不得静默丢弃（硬约束 C8）。
        for table_index, table in enumerate(document.tables, start=1):
            for row_index, row in enumerate(table.rows, start=1):
                cells = [cell.text.strip() for cell in row.cells]
                if not any(cells):
                    continue
                text = f"表格{table_index} 第{row_index}行：" + "｜".join(cells)
                blocks.append(
                    ParsedBlock(
                        text=text,
                        kind="table_row",
                        table_id=f"docx-table-{table_index}",
                        metadata={"order": order.get(table._element, len(order)), "row": row_index},
                        locator=Locator(
                            type="docx",
                            block=table_index,
                            char_start=0,
                            char_end=len(text),
                            precision="block",
                        ),
                    )
                )
        if has_images and blocks:
            blocks[0] = replace(blocks[0], warnings=("docx_images_require_mineru",))
        return blocks


XLSX_TRUNCATED = "xlsx_rows_truncated"


class XlsxParser:
    """把工作表按行展开为文本：一行一条记录，制表符分隔单元格。

    不重建表头语境：块头标注工作表名与行区间，检索命中后能回到具体行。
    合并单元格只保留左上角的值（openpyxl 行为），其余为空。
    """

    name = "xlsx"

    def __init__(self, max_rows: int = 5000, rows_per_block: int = 40) -> None:
        self.max_rows = max_rows
        self.rows_per_block = max(1, rows_per_block)

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == XLSX

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        import openpyxl

        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise ParseFailed("表格无法读取或已加密") from exc

        blocks: list[ParsedBlock] = []
        read_rows = 0
        truncated = False
        try:
            for sheet in workbook.worksheets:
                title = sheet.title or "Sheet"
                batch: list[str] = []
                row_numbers: list[int] = []
                batch_start = 0
                row_number = 0
                for row in sheet.iter_rows(values_only=True):
                    row_number += 1
                    cells = [cell_text(value) for value in row]
                    if not any(cells):
                        continue
                    if read_rows >= self.max_rows:
                        truncated = True
                        break
                    read_rows += 1
                    if not batch:
                        batch_start = row_number
                    batch.append("\t".join(cells).rstrip())
                    row_numbers.append(row_number)
                    if len(batch) >= self.rows_per_block:
                        blocks.append(_sheet_block(title, batch_start, row_number, batch, row_numbers))
                        batch = []
                        row_numbers = []
                if batch:
                    blocks.append(_sheet_block(title, batch_start, row_number, batch, row_numbers))
                if truncated:
                    break
        finally:
            workbook.close()

        if not blocks:
            raise ParseFailed("表格中没有可提取的内容")
        if truncated:
            last = blocks[-1]
            blocks[-1] = replace(last, warnings=tuple(sorted({*last.warnings, XLSX_TRUNCATED})))
        return blocks


def cell_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, datetime):
        return value.date().isoformat() if value.time() == time.min else value.isoformat()
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value).replace("\n", " ").strip()


CSV_TRUNCATED = "csv_rows_truncated"


class CsvParser:
    """逗号分隔文本按行展开，与 XLSX 一样用工作表/行区间定位。"""

    name = "csv"

    def __init__(self, max_rows: int = 5000, rows_per_block: int = 40) -> None:
        self.max_rows = max_rows
        self.rows_per_block = max(1, rows_per_block)

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == TEXT_CSV

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        text = decode_table_text(data)
        if text is None:
            raise ParseFailed("表格文本编码无法识别")
        blocks: list[ParsedBlock] = []
        batch: list[str] = []
        row_numbers: list[int] = []
        batch_start = 0
        row_number = 0
        read_rows = 0
        truncated = False
        for row in csv.reader(io.StringIO(text)):
            row_number += 1
            cells = [cell.strip() for cell in row]
            if not any(cells):
                continue
            if read_rows >= self.max_rows:
                truncated = True
                break
            read_rows += 1
            if not batch:
                batch_start = row_number
            batch.append("\t".join(cells).rstrip())
            row_numbers.append(row_number)
            if len(batch) >= self.rows_per_block:
                blocks.append(_csv_block(batch_start, row_number, batch, row_numbers))
                batch = []
                row_numbers = []
        if batch:
            blocks.append(_csv_block(batch_start, row_number, batch, row_numbers))
        if not blocks:
            raise ParseFailed("表格中没有可提取的内容")
        if truncated:
            last = blocks[-1]
            blocks[-1] = replace(last, warnings=tuple(sorted({*last.warnings, CSV_TRUNCATED})))
        return blocks


def _csv_block(start: int, end: int, rows: list[str], row_numbers: list[int] | None = None) -> ParsedBlock:
    text = f"CSV 第{start}-{end}行\n" + "\n".join(rows)
    return ParsedBlock(
        text=text,
        kind="table",
        table_id="CSV",
        metadata={"row_numbers": row_numbers or []},
        locator=Locator(
            type="csv",
            sheet="CSV",
            row_start=start,
            row_end=end,
            char_start=0,
            char_end=len(text),
            precision="sheet",
        ),
    )


class PptxParser:
    """只读 OOXML 幻灯片文本；不渲染版式，也不引入 Office 二进制依赖。"""

    name = "pptx"
    _SLIDE = re.compile(r"ppt/slides/slide(\d+)\.xml$")
    _A = "{http://schemas.openxmlformats.org/drawingml/2006/main}"

    def __init__(
        self, max_entries: int = 2048, max_uncompressed_bytes: int = 100 * 1024 * 1024
    ) -> None:
        self.max_entries = max_entries
        self.max_uncompressed_bytes = max_uncompressed_bytes

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == PPTX

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        from defusedxml import ElementTree

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > self.max_entries
                or sum(entry.file_size for entry in entries) > self.max_uncompressed_bytes
            ):
                raise ParseFailed("PPTX 解压资源超限")
            slides = sorted(
                (
                    (int(match.group(1)), entry.filename)
                    for entry in entries
                    if (match := self._SLIDE.match(entry.filename))
                ),
                key=lambda item: item[0],
            )
            if not slides:
                raise ParseFailed("演示文稿中没有可提取的幻灯片")
            blocks: list[ParsedBlock] = []
            for page, name in slides:
                root = ElementTree.fromstring(archive.read(name))
                paragraphs: list[str] = []
                for paragraph in root.iter(self._A + "p"):
                    text = "".join(
                        node.text or "" for node in paragraph.iter(self._A + "t")
                    ).strip()
                    if text:
                        paragraphs.append(text)
                if not paragraphs:
                    continue
                text = "\n".join(paragraphs)
                blocks.append(
                    ParsedBlock(
                        text=text,
                        locator=Locator(
                            type="pptx",
                            page=page,
                            char_start=0,
                            char_end=len(text),
                            precision="block",
                        ),
                    )
                )
        if not blocks:
            raise ParseFailed("演示文稿中没有可提取的文本")
        return blocks


def _sheet_block(
    sheet: str, start: int, end: int, rows: list[str], row_numbers: list[int] | None = None,
) -> ParsedBlock:
    header = f"工作表《{sheet}》第{start}-{end}行"
    text = header + "\n" + "\n".join(rows)
    return ParsedBlock(
        text=text,
        kind="table",
        table_id=sheet,
        metadata={"row_numbers": row_numbers or []},
        locator=Locator(
            type="xlsx",
            sheet=sheet,
            row_start=start,
            row_end=end,
            char_start=0,
            char_end=len(text),
            precision="sheet",
        ),
    )


class ParserRegistry:
    def __init__(
        self,
        parsers: list,
        *,
        profile: str = "legacy-v1",
        mineru: Parser | None = None,
        direct_text: Parser | None = None,
    ) -> None:
        self._parsers = parsers
        self.profile = profile
        self._mineru = mineru
        self._direct_text = direct_text or DirectTextParser()

    def for_file(self, media_type: str, filename: str, *, profile: str | None = None):
        selected = profile or self.profile
        if selected not in {"legacy-v1", "mineru-api4.1-v1"}:
            raise ParseFailed("处理策略版本不受支持")
        if selected == "mineru-api4.1-v1":
            if media_type in {TEXT_PLAIN, TEXT_MARKDOWN}:
                return self._direct_text
            if self._mineru is None or not self._mineru.supports(media_type, filename):
                raise UnsupportedFormat("所选解析器未配置或不支持该文件")
            return self._mineru
        for parser in self._parsers:
            if parser.supports(media_type, filename):
                return parser
        raise UnsupportedFormat(f"暂不支持该格式：{media_type}")


class DirectTextParser(TextParser):
    name = "direct-text"

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        return [
            ParsedBlock(b.text, b.locator, (*b.warnings, "parser_direct_text"))
            for b in super().parse(data, media_type, filename)
        ]


def build_parser_registry(settings: Settings | None = None) -> ParserRegistry:
    from knowledge.adapters.mineru import MinerUParser
    from knowledge.adapters.parse_process import BoundedParser

    resolved = settings or Settings.model_construct()
    parsers: list[Parser] = [
        TextParser(),
        CsvParser(resolved.xlsx_max_rows, resolved.xlsx_rows_per_block),
        PdfParser(),
        DocxParser(resolved.docx_max_entries, resolved.docx_max_uncompressed_bytes),
        PptxParser(resolved.docx_max_entries, resolved.docx_max_uncompressed_bytes),
        XlsxParser(resolved.xlsx_max_rows, resolved.xlsx_rows_per_block),
    ]
    # 远端调用自身有超时；服务端负责可终止的重型解析，不受本地 512MiB 上限误杀。
    return ParserRegistry(
        [BoundedParser(parser, resolved) for parser in parsers],
        profile=resolved.document_parser_profile,
        mineru=MinerUParser(resolved),
        direct_text=BoundedParser(DirectTextParser(), resolved),
    )
