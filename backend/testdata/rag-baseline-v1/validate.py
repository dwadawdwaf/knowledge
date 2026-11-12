#!/usr/bin/env python3
"""校验 rag-baseline-v1 的冻结哈希、查询计数与金标准可定位性。"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import unicodedata

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

ROOT = pathlib.Path(__file__).resolve().parent


def normalized(value: str) -> str:
    return "".join(unicodedata.normalize("NFKC", value).lower().split())


def text_of(document: dict) -> str:
    path = ROOT / document["file"]
    fmt = document["format"]
    if fmt == "pdf":
        return "\n".join((page.extract_text() or "") for page in PdfReader(str(path)).pages)
    if fmt == "docx":
        parsed = Document(str(path))
        parts = [paragraph.text for paragraph in parsed.paragraphs]
        for table in parsed.tables:
            for row in table.rows:
                parts.extend(cell.text for cell in row.cells)
        return "\n".join(parts)
    if fmt == "xlsx":
        workbook = load_workbook(str(path), read_only=True, data_only=True)
        parts = []
        for sheet in workbook.worksheets:
            for row in sheet.iter_rows(values_only=True):
                parts.extend(str(value) for value in row if value is not None)
        return "\n".join(parts)
    if fmt == "pptx":
        presentation = Presentation(str(path))
        parts = []
        for slide in presentation.slides:
            for shape in slide.shapes:
                if shape.has_text_frame:
                    parts.append(shape.text_frame.text)
                if shape.has_table:
                    for row in shape.table.rows:
                        parts.extend(cell.text for cell in row.cells)
        return "\n".join(parts)
    return path.read_text(encoding="utf-8", errors="ignore")


def main() -> int:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    documents = {document["id"]: document for document in manifest["documents"]}
    failures: list[str] = []
    for document in manifest["documents"]:
        digest = hashlib.sha256((ROOT / document["file"]).read_bytes()).hexdigest()
        if digest != document["sha256"]:
            failures.append(f"哈希不一致：{document['id']}")
    for query in manifest["queries"]:
        if not query["answerable"] or not query["doc"]:
            continue
        document = documents[query["doc"]]
        if document["format"] == "png":
            continue  # 需要 OCR，交给 MinerU 验收
        text = normalized(text_of(document))
        for needle in query["gold"]["needles"]:
            if normalized(needle) not in text:
                failures.append(f"金标准不可定位：{query['id']} -> {needle}")
    expected = {"documents": 96, "queries": 228, "positive": 208, "negative": 20}
    counts = {
        "documents": len(manifest["documents"]),
        "queries": len(manifest["queries"]),
        "positive": sum(1 for query in manifest["queries"] if query["answerable"]),
        "negative": sum(1 for query in manifest["queries"] if not query["answerable"]),
    }
    for key, value in expected.items():
        if counts[key] != value:
            failures.append(f"计数不一致：{key}={counts[key]}，期望 {value}")
    if failures:
        print("\n".join(failures[:50]))
        print(f"FAILED {len(failures)}")
        return 1
    print(json.dumps(counts, ensure_ascii=False))
    print("OK")
    return 0


if __name__ == "__main__":
    sys.exit(main())
