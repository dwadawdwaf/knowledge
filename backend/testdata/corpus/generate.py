"""评测语料的 DOCX / PDF 生成：内容定义在 labels.py，测试与标定脚本共用。"""

from __future__ import annotations

import io
import os
from pathlib import Path

from testdata.corpus.labels import DOCX_PARAGRAPHS, PDF_PAGES

FONT_PATH = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simhei.ttf"


def make_docx() -> bytes:
    import docx

    document = docx.Document()
    for paragraph in DOCX_PARAGRAPHS:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def make_pdf() -> bytes:
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_font("hei", "", str(FONT_PATH))
    pdf.set_font("hei", size=12)
    for page_lines in PDF_PAGES:
        pdf.add_page()
        for line in page_lines:
            # new_x=LMARGIN：multi_cell 结束后回到左边距，否则下一行可用宽度为 0
            pdf.multi_cell(0, 8, line, new_x="LMARGIN", new_y="NEXT")
    return bytes(pdf.output())


def pdf_supported() -> bool:
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return False
    return FONT_PATH.is_file()
