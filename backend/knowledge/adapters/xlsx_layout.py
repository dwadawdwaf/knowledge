"""从只读 XLSX 的同一工作表部件提取有界的合并范围。"""

from __future__ import annotations

import io
import zipfile
from xml.sax import SAXException
from xml.sax.handler import ContentHandler, feature_namespaces

from defusedxml.common import DefusedXmlException
from defusedxml.sax import make_parser
from openpyxl.utils.cell import range_boundaries
from openpyxl.worksheet._read_only import ReadOnlyWorksheet

MERGE_SCAN_BYTES = 16 * 1024 * 1024
_MAIN = "http://schemas.openxmlformats.org/spreadsheetml/2006/main"


class _Ranges(ContentHandler):
    def __init__(self) -> None:
        super().__init__()
        self.depth = 0
        self.container = False
        self.ranges: list[tuple[int, int, int, int]] = []

    def startElementNS(self, name, qname, attrs) -> None:
        self.depth += 1
        if self.depth == 2 and name == (_MAIN, "mergeCells"):
            self.container = True
        elif self.depth == 3 and self.container and name == (_MAIN, "mergeCell"):
            first_col, first_row, last_col, last_row = range_boundaries(attrs.get((None, "ref"), ""))
            if not (
                first_col and first_row and last_col and last_row
                and 1 <= first_col <= last_col <= 16384 and 1 <= first_row <= last_row <= 1_048_576
                and len(self.ranges) < 4096
            ):
                raise ValueError("合并范围不可用")
            self.ranges.append((first_col, first_row, last_col, last_row))

    def endElementNS(self, name, qname) -> None:
        if self.depth == 2:
            self.container = False
        self.depth -= 1


def read_sheet_merges(
    sheet: ReadOnlyWorksheet, remaining: int,
) -> tuple[list[tuple[int, int, int, int]] | None, int]:
    """额外读取只消耗共享预算，无法核验时由调用者保留普通行解析。"""
    if remaining <= 0:
        return None, 0
    try:
        # openpyxl 已解析工作表关系，此入口打开当前 sheet 对应的 ZIP 部件。
        with sheet._get_source() as source:
            data = source.read(remaining + 1)
    except (AttributeError, KeyError, OSError, zipfile.BadZipFile):
        return None, remaining
    if len(data) > remaining:
        return None, 0
    remaining -= len(data)
    handler = _Ranges()
    try:
        parser = make_parser()
        parser.forbid_dtd = True
        parser.setFeature(feature_namespaces, True)
        parser.setContentHandler(handler)
        parser.parse(io.BytesIO(data))
    except (DefusedXmlException, SAXException, TypeError, ValueError):
        return None, remaining
    return handler.ranges, remaining
