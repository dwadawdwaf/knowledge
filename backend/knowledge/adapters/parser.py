"""解析器：原文件 → 有序内容块。不判断业务规则，只交付文本与定位。"""

from __future__ import annotations

import io
import zipfile

from knowledge.config import Settings
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed, PasswordProtected, UnsupportedFormat
from knowledge.domain.ports import Parser

TEXT_PLAIN = "text/plain"
TEXT_MARKDOWN = "text/markdown"
APPLICATION_PDF = "application/pdf"
DOCX = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
OCTET_STREAM = "application/octet-stream"

_EXT_MAP = {
    ".txt": TEXT_PLAIN,
    ".md": TEXT_MARKDOWN,
    ".markdown": TEXT_MARKDOWN,
    ".pdf": APPLICATION_PDF,
    ".docx": DOCX,
}


def detect_media_type(data: bytes, filename: str) -> str:
    """按内容签名判断类型，扩展名只作兜底。"""
    if data.startswith(b"%PDF"):
        return APPLICATION_PDF
    if data.startswith(b"PK\x03\x04") and filename.lower().endswith(".docx"):
        return DOCX

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
    return OCTET_STREAM


def _split_paragraphs(text: str) -> list[tuple[int, int]]:
    """按空行切块，返回原文偏移区间。"""
    spans: list[tuple[int, int]] = []
    start: int | None = None
    i = 0
    length = len(text)

    while i < length:
        char = text[i]
        if char in " \t\r\n":
            if start is not None and char == "\n" and text[i : i + 2] == "\n\n":
                spans.append((start, i))
                start = None
                i += 2
                continue
            i += 1
            continue
        if start is None:
            start = i
        i += 1

    if start is not None:
        spans.append((start, length))
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
        document = docx.Document(io.BytesIO(data))
        blocks: list[ParsedBlock] = []
        for index, paragraph in enumerate(document.paragraphs):
            text = paragraph.text
            if not text.strip():
                continue
            blocks.append(
                ParsedBlock(
                    text=text,
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
                        locator=Locator(
                            type="docx",
                            block=table_index,
                            char_start=0,
                            char_end=len(text),
                            precision="block",
                        ),
                    )
                )
        return blocks


class ParserRegistry:
    def __init__(self, parsers: list) -> None:
        self._parsers = parsers

    def for_file(self, media_type: str, filename: str):
        for parser in self._parsers:
            if parser.supports(media_type, filename):
                return parser
        raise UnsupportedFormat(f"暂不支持该格式：{media_type}")


def build_parser_registry(settings: Settings | None = None) -> ParserRegistry:
    from knowledge.adapters.parse_process import BoundedParser

    resolved = settings or Settings.model_construct()
    parsers: list[Parser] = [
        TextParser(),
        PdfParser(),
        DocxParser(resolved.docx_max_entries, resolved.docx_max_uncompressed_bytes),
    ]
    return ParserRegistry([BoundedParser(parser, resolved) for parser in parsers])
