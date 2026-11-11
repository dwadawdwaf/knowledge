"""解析器：原文件 → 有序内容块。不判断业务规则，只交付文本与定位。"""

from __future__ import annotations

import csv
import io
import re
import zipfile
from collections.abc import Iterator
from dataclasses import dataclass, replace
from datetime import datetime, time

from knowledge.adapters.markdown_layout import markdown_tables
from knowledge.config import Settings
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed, PasswordProtected, UnsupportedFormat
from knowledge.domain.ports import Parser
from knowledge.domain.ragflow_table import TableRow

TEXT_PLAIN = "text/plain"
TEXT_MARKDOWN = "text/markdown"
TEXT_CSV = "text/csv"
APPLICATION_PDF = "application/pdf"
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
    """按空行切块，返回原文偏移区间。"""
    # 不先替换换行：偏移必须仍能切回原始解码文本，包括 CRLF 中的两个码点。
    spans: list[tuple[int, int]] = []
    cursor = 0
    for separator in re.finditer(r"\r?\n[ \t]*\r?\n(?:[ \t]*\r?\n)*", text):
        start = cursor + len(text[cursor:separator.start()]) - len(text[cursor:separator.start()].lstrip(" \t\r\n"))
        if text[start:separator.start()].strip():
            spans.append((start, separator.start()))
        cursor = separator.end()
    start = cursor + len(text[cursor:]) - len(text[cursor:].lstrip(" \t\r\n"))
    if text[start:].strip():
        spans.append((start, len(text)))
    return spans


@dataclass(frozen=True)
class _TextSection:
    start: int
    end: int
    kind: str = "text"
    rows: list[TableRow] | None = None


class TextParser:
    name = "text"

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type in (TEXT_PLAIN, TEXT_MARKDOWN)

    def _sections(self, text: str) -> Iterator[_TextSection]:
        yield from (_TextSection(start, end) for start, end in _split_paragraphs(text))

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        text = data.decode("utf-8", errors="replace")
        blocks: list[ParsedBlock] = []
        for index, section in enumerate(self._sections(text)):
            piece = text[section.start:section.end]
            if not piece.strip():
                continue
            blocks.append(
                ParsedBlock(
                    text=piece,
                    locator=Locator(
                        type="md" if media_type == TEXT_MARKDOWN else "txt",
                        char_start=section.start,
                        char_end=section.end,
                        precision="char",
                        block=index,
                    ),
                    kind=section.kind,
                    table_id=f"markdown-{section.start}" if section.rows is not None else None,
                    metadata={"table_rows": section.rows} if section.rows is not None else {},
                )
            )
        return blocks


_MARKDOWN_FENCE = re.compile(r"(?:\A\ufeff?|(?<=\n)|(?<=\r)) {0,3}(`{3,}|~{3,})([^\r\n]*)(?:\r\n|[\r\n]|\Z)")


class MarkdownStructureParser(TextParser):
    """先识别代码围栏，再解析外围表格与正文，保留原始解码文本的范围。"""

    name = "markdown-structure"

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == TEXT_MARKDOWN

    def _prose_sections(self, text: str, origin: int) -> Iterator[_TextSection]:
        cursor = 0
        for start, end, rows in markdown_tables(text):
            yield from (replace(part, start=origin + cursor + part.start, end=origin + cursor + part.end)
                        for part in super()._sections(text[cursor:start]))
            yield _TextSection(origin + start, origin + end, "table", rows)
            cursor = end
        yield from (replace(part, start=origin + cursor + part.start, end=origin + cursor + part.end)
                    for part in super()._sections(text[cursor:]))

    def _sections(self, text: str) -> Iterator[_TextSection]:
        cursor = 0
        opened: tuple[str, int] | None = None
        for match in _MARKDOWN_FENCE.finditer(text):
            fence, trailing = match.groups()
            if opened is not None:
                marker, start = opened
                if fence[0] == marker[0] and len(fence) >= len(marker) and not trailing.strip(" \t"):
                    yield _TextSection(start, match.end(), "code")
                    cursor = match.end()
                    opened = None
            elif fence[0] != "`" or "`" not in trailing:
                yield from self._prose_sections(text[cursor:match.start()], cursor)
                opened = fence, match.start()
        if opened is not None:
            yield _TextSection(opened[1], len(text), "code")
        else:
            yield from self._prose_sections(text[cursor:], cursor)


class PdfParser:
    name = "pdf"

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == APPLICATION_PDF

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PasswordProtected("PDF 已加密，无法解析")
        return [
            block
            for index, page in enumerate(reader.pages, start=1)
            for block in self._page_blocks(index, page.extract_text() or "")
        ]

    @staticmethod
    def _page_blocks(page_index: int, page_text: str) -> Iterator[ParsedBlock]:
        for index, (start, end) in enumerate(_split_paragraphs(page_text)):
            piece = page_text[start:end]
            if piece.strip():
                yield ParsedBlock(
                    text=piece,
                    locator=Locator(
                        type="pdf", page=page_index, block=index,
                        char_start=start, char_end=end, precision="block",
                    ),
                )


class PlainPdfProbe(PdfParser):
    """仅接受可确认的单栏、可见文本。空列表表示交给 MinerU。"""

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        from pypdf import PdfReader

        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PasswordProtected("PDF 已加密，无法解析")
        if not 0 < len(reader.pages) <= 64:
            return []
        blocks: list[ParsedBlock] = []
        for index, page in enumerate(reader.pages, start=1):
            stream = page.get_contents()
            if page.rotation or stream is None:
                return []
            for operands, operator in stream.operations:
                if operator in {
                    b"Do", b"INLINE IMAGE", b"S", b"s", b"f", b"F", b"f*",
                    b"B", b"B*", b"b", b"b*", b"sh",
                }:
                    return []
                if operator == b"Tr" and operands[0] != 0:
                    return []
                if operator in {b"Tm", b"cm"} and (
                    operands[1] or operands[2] or operands[0] <= 0 or operands[3] <= 0
                ):
                    return []
            layout = page.extract_text(extraction_mode="layout")
            lines = [line.rstrip() for line in layout.splitlines() if line.strip()]
            if not lines or sum(char.isalnum() for char in layout) < 12 or "\ufffd" in layout:
                return []
            margins = [len(line) - len(line.lstrip()) for line in lines]
            if max(margins) - min(margins) > 4 or any(
                re.search(r"\S {4,}\S|[∑∫√]", line) for line in lines
            ):
                return []
            plain = page.extract_text() or ""
            if not plain.strip() or "\ufffd" in plain or "\x00" in plain:
                return []
            # 绘制顺序和视觉顺序不一致时，简单本地提取不能保留可靠的阅读顺序。
            if "".join(plain.split()) != "".join(layout.split()):
                return []
            blocks.extend(self._page_blocks(index, plain))
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
        from docx.oxml.ns import qn

        with zipfile.ZipFile(io.BytesIO(data)) as archive:
            entries = archive.infolist()
            if (
                len(entries) > self.max_entries
                or sum(entry.file_size for entry in entries) > self.max_uncompressed_bytes
            ):
                raise ParseFailed("DOCX 解压资源超限")
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
        # 正文沿用原来的行文本，网格和表头标记供结构分块使用。
        for table_index, table in enumerate(document.tables, start=1):
            width = len(table.columns)
            for row_index, row in enumerate(table.rows, start=1):
                cells = row.cells
                values = [cell.text.strip() for cell in cells]
                if not any(values):
                    continue
                text = f"表格{table_index} 第{row_index}行：" + "｜".join(values)
                header = row._tr.find(f"{qn('w:trPr')}/{qn('w:tblHeader')}")
                explicit_header = header is not None and header.get(qn("w:val")) not in {"0", "false", "off"}
                grid = None
                if 0 < width <= 256 and len(cells) == width and not (row.grid_cols_before or row.grid_cols_after):
                    grid = [
                        (value, cell._tc.grid_span == 1 and cell._tc.getparent() is row._tr)
                        for value, cell in zip(values, cells, strict=True)
                    ]
                blocks.append(
                    ParsedBlock(
                        text=text,
                        kind="table_row",
                        table_id=f"docx-table-{table_index}",
                        metadata={
                            "order": order.get(table._element, len(order)), "row": row_index,
                            "is_header": row_index == 1 and explicit_header,
                            "docx_header": explicit_header, "docx_grid": grid,
                        },
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


XLSX_TRUNCATED = "xlsx_rows_truncated"
XLSX_MERGE_UNAVAILABLE = "xlsx_merge_structure_unavailable"


class XlsxParser:
    """把工作表按行展开为文本：一行一条记录，制表符分隔单元格。

    块头标注工作表和行区间，正文仅保留原单元格的值。
    合并范围和单元格边界单独作为分块元数据。
    """

    name = "xlsx"

    def __init__(self, max_rows: int = 5000, rows_per_block: int = 40) -> None:
        self.max_rows = max_rows
        self.rows_per_block = max(1, rows_per_block)

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type == XLSX

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        import openpyxl

        from knowledge.adapters.xlsx_layout import MERGE_SCAN_BYTES, read_sheet_merges

        try:
            workbook = openpyxl.load_workbook(io.BytesIO(data), read_only=True, data_only=True)
        except Exception as exc:
            raise ParseFailed("表格无法读取或已加密") from exc

        blocks: list[ParsedBlock] = []
        read_rows = 0
        merge_budget = MERGE_SCAN_BYTES
        truncated = False
        try:
            for sheet in workbook.worksheets:
                title = sheet.title or "Sheet"
                merges, merge_budget = read_sheet_merges(sheet, merge_budget)
                structured = bool(merges and any(first_row <= 2 for _, first_row, _, _ in merges))
                first_block = len(blocks)
                preview: list[list[str]] = []
                batch: list[str] = []
                row_numbers: list[int] = []
                cell_lengths: list[list[int] | None] = []
                batch_start = 0
                row_number = 0
                for row in sheet.iter_rows(values_only=True):
                    row_number += 1
                    cells = [cell_text(value) for value in row]
                    supported_width = not structured or not any(cells[256:])
                    if structured and row_number <= 6:
                        preview.append(cells[:256] if supported_width and all(len(cell) <= 512 for cell in cells) else [])
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
                    if structured:
                        # 普通行可从制表符恢复。仅单元格内已有制表符时记录真实列边界。
                        if not supported_width:
                            cell_lengths.append([])
                        elif any("\t" in cell for cell in cells):
                            cell_lengths.append(list(map(len, cells[:256])))
                        else:
                            cell_lengths.append(None)
                    if len(batch) >= self.rows_per_block:
                        blocks.append(_sheet_block(title, batch_start, row_number, batch, row_numbers, cell_lengths))
                        batch = []
                        row_numbers = []
                        cell_lengths = []
                if batch:
                    blocks.append(_sheet_block(title, batch_start, row_number, batch, row_numbers, cell_lengths))
                if first_block < len(blocks):
                    block = blocks[first_block]
                    if structured:
                        blocks[first_block] = replace(
                            block, metadata={**block.metadata, "xlsx_merge_ranges": merges, "xlsx_preview": preview},
                        )
                    elif merges is None:
                        blocks[first_block] = replace(block, warnings=(*block.warnings, XLSX_MERGE_UNAVAILABLE))
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
        batch: list[list[str]] = []
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
            batch.append(cells)
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


def _csv_block(start: int, end: int, rows: list[list[str]], row_numbers: list[int]) -> ParsedBlock:
    prefix = f"CSV 第{start}-{end}行\n"
    rendered = []
    spans = []
    cursor = len(prefix)
    for cells in rows:
        line = "\t".join(cells).rstrip()
        rendered.append(line)
        spans.append((cursor, cursor + len(line), [len(cell) for cell in cells]))
        cursor += len(line) + 1
    text = prefix + "\n".join(rendered)
    return ParsedBlock(
        text=text,
        kind="table",
        table_id="CSV",
        metadata={"row_numbers": row_numbers, "csv_row_spans": spans},
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
    cell_lengths: list[list[int] | None] | None = None,
) -> ParsedBlock:
    header = f"工作表《{sheet}》第{start}-{end}行"
    text = header + "\n" + "\n".join(rows)
    return ParsedBlock(
        text=text,
        kind="table",
        table_id=sheet,
        metadata={"row_numbers": row_numbers or [], **({"xlsx_cell_lengths": cell_lengths} if cell_lengths else {})},
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
        pdf_probe: Parser | None = None,
        structured_markdown: Parser | None = None,
    ) -> None:
        self._parsers = parsers
        self.profile = profile
        self._mineru = mineru
        self._direct_text = direct_text or DirectTextParser()
        self._pdf_probe = pdf_probe
        self._structured_markdown = structured_markdown

    def probe_pdf(self, data: bytes, filename: str) -> list[ParsedBlock]:
        if self._pdf_probe is None:
            return []
        try:
            return self._pdf_probe.parse(data, APPLICATION_PDF, filename)
        except ParseFailed:
            return []

    def for_file(
        self, media_type: str, filename: str, *, profile: str | None = None, structured_markdown: bool = False,
    ):
        selected = profile or self.profile
        if selected not in {"legacy-v1", "mineru-api4.1-v1"}:
            raise ParseFailed("处理策略版本不受支持")
        if structured_markdown and media_type == TEXT_MARKDOWN and self._structured_markdown is not None:
            return self._structured_markdown
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
        structured_markdown=BoundedParser(MarkdownStructureParser(), resolved),
        pdf_probe=BoundedParser(PlainPdfProbe(), resolved.model_copy(update={
            "parse_timeout_seconds": min(5.0, resolved.parse_timeout_seconds),
            "parse_cpu_seconds": min(5.0, resolved.parse_cpu_seconds),
        })),
    )
