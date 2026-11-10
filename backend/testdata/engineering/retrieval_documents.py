"""生成用于引用定位回归的自制 DOCX 和 PDF。"""

from __future__ import annotations

import io
import os
from pathlib import Path

FONT_PATH = Path(
    os.environ.get(
        "KS_TEST_FONT_PATH",
        str(Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simhei.ttf"),
    )
)

DOCX_PARAGRAPHS = [
    "数据备份管理细则（自制测试资料）",
    "第一条 核心业务数据每日凌晨执行一次全量备份。",
    "第二条 备份文件保留 30 天，并在异地存放一份。",
]

PDF_PAGES = [
    [
        "第一条 所有访客须在前台登记并领取访客证。",
        "第二条 接待人须全程陪同访客进入办公区。",
    ],
    [
        "第三条 访客证当日有效，离开时交回前台。",
        "第四条 外来车辆须提前报备车牌号，停放在访客专区。",
    ],
    ["第五条 消防通道内不得堆放任何物品，安全出口指示灯须保持常亮。"],
    ["第六条 末班班车发车时间为晚上十点半，路线覆盖主要居住区。"],
    ["第七条 监控录像保存九十天，调阅须经行政部审批。"],
]


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
