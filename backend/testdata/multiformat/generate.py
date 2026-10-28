"""生成多种格式的自制样本，用于验证 DeepDoc 的版面、表格与 OCR 能力。

全部内容为自撰示例，不含真实单位信息，也不引用受版权保护的文本。
用法：`python testdata/multiformat/generate.py`
"""

from __future__ import annotations

import pathlib

from docx import Document
from openpyxl import Workbook
from openpyxl.styles import Font
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Pt
from fpdf import FPDF
from PIL import Image, ImageDraw, ImageFont

HERE = pathlib.Path(__file__).resolve().parent
FONT_REGULAR = "C:/Windows/Fonts/msyh.ttc"
FONT_BOLD = "C:/Windows/Fonts/msyhbd.ttc"
PAGE = (1654, 2339)  # A4 @ 200 DPI


def _pdf_font(pdf: FPDF, family: str, style: str, path: str) -> None:
    pdf.add_font(family, style, path)


def text_pdf() -> pathlib.Path:
    """双页文本层 PDF：标题、编号条款、一张跨列标准表。"""
    pdf = FPDF(format="A4")
    _pdf_font(pdf, "MS", "", FONT_REGULAR)
    _pdf_font(pdf, "MS", "B", FONT_BOLD)
    pdf.set_auto_page_break(auto=True, margin=18)
    pdf.add_page()

    pdf.set_font("MS", "B", 20)
    pdf.cell(0, 12, "差旅费报销管理办法", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_font("MS", "", 10)
    pdf.set_text_color(90, 90, 90)
    pdf.cell(0, 7, "文号：自撰示例〔2026〕第 12 号", new_x="LMARGIN", new_y="NEXT", align="C")
    pdf.set_text_color(0, 0, 0)
    pdf.ln(4)

    body = [
        ("第一条", "本办法适用于本单位因公出差人员的交通、住宿与伙食补助管理。"),
        ("第二条", "出差前应提交行程单，注明目的地、事由、起止日期与预计费用，经部门负责人批准后方可成行。"),
        ("第三条", "市内公务出行优先使用公共交通，凭据实报实销；确需使用网约车的，应在报销单注明起止地点。"),
        ("第四条", "住宿费按城市分级标准执行，超出标准部分由个人承担，不得以其他名目列入报销。"),
        ("第五条", "伙食补助按自然日计算，出发当日与返回当日各计半天。"),
    ]
    pdf.set_font("MS", "", 12)
    for number, content in body:
        # 编号与正文放在同一文本段：拆成两个 cell 会被版面模型识别成两栏。
        pdf.multi_cell(0, 8, f"{number}　{content}")
        pdf.ln(1)

    pdf.set_font("MS", "", 11)
    pdf.multi_cell(0, 8, "第六条　报销单据应在出差结束后十个工作日内提交，逾期需附书面说明。")
    pdf.ln(1)
    pdf.multi_cell(0, 8, "第七条　同一行程不得重复报销；票据遗失须提供情况说明与付款记录。")

    # 表格放在页尾：紧邻表格的正文会被表格区域吞并，实测过这一现象。
    pdf.ln(8)
    pdf.set_font("MS", "B", 12)
    pdf.cell(0, 8, "附表：住宿费分级标准（元／人·晚）", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)

    rows = [
        ("城市分级", "适用范围", "一般人员", "部门负责人"),
        ("一类", "直辖市、省会城市", "600", "800"),
        ("二类", "计划单列市、经济特区", "480", "640"),
        ("三类", "其他地级市", "380", "500"),
        ("四类", "县级及以下", "280", "380"),
    ]
    widths = (28, 72, 34, 34)
    pdf.set_font("MS", "B", 11)
    for row in rows[:1]:
        for width, cell in zip(widths, row):
            pdf.cell(width, 9, cell, border=1, align="C")
        pdf.ln()
    pdf.set_font("MS", "", 11)
    for row in rows[1:]:
        for width, cell in zip(widths, row):
            pdf.cell(width, 9, cell, border=1, align="C")
        pdf.ln()

    pdf.add_page()
    pdf.set_font("MS", "B", 15)
    pdf.cell(0, 10, "第八条 特殊情形处理", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("MS", "", 12)
    for text in (
        "（一）因航班取消或天气原因滞留，凭承运人证明据实增加一晚住宿。",
        "（二）陪同外部来访人员的接待费用单独列示，不占用差旅标准。",
        "（三）跨年度出差按出发年度标准执行，不得跨年度拆分报销。",
        "（四）境外差旅另行规定，本办法不适用。",
    ):
        pdf.multi_cell(0, 8, text)
        pdf.ln(1)

    pdf.ln(4)
    pdf.set_font("MS", "B", 15)
    pdf.cell(0, 10, "第九条 附则", new_x="LMARGIN", new_y="NEXT")
    pdf.ln(2)
    pdf.set_font("MS", "", 12)
    pdf.multi_cell(0, 8, "本办法由行政与财务共同解释，自发布之日起施行。此前的差旅规定同时废止。")

    path = HERE / "制度-差旅费报销管理办法.pdf"
    pdf.output(str(path))
    return path


def _page_image(title: str, lines: list[str], table: list[list[str]] | None = None) -> Image.Image:
    image = Image.new("RGB", PAGE, "white")
    draw = ImageDraw.Draw(image)
    title_font = ImageFont.truetype(FONT_BOLD, 52)
    body_font = ImageFont.truetype(FONT_REGULAR, 36)
    small_font = ImageFont.truetype(FONT_REGULAR, 30)

    draw.text((120, 110), title, font=title_font, fill="black")
    draw.line((120, 190, 1530, 190), fill="black", width=3)
    y = 230
    for line in lines:
        draw.text((120, y), line, font=body_font, fill="black")
        y += 62
    if table:
        y += 40
        col_x = [120, 560, 980, 1300, 1530]
        row_h = 62
        for r, row in enumerate(table):
            for i, cell in enumerate(row):
                draw.rectangle((col_x[i], y, col_x[i + 1], y + row_h), outline="black", width=3)
                draw.text((col_x[i] + 16, y + 12), cell, font=small_font, fill="black")
            y += row_h
    return image


def scanned_pdf() -> pathlib.Path:
    """单页扫描版 PDF：无文本层，必须走 OCR。"""
    image = _page_image(
        "设备巡检记录表",
        [
            "巡检区域：三层机房　　巡检日期：2026 年 10 月 21 日",
            "巡检人：值班组　　审核人：设备主管",
        ],
        [
            ["序号", "检查项", "结果", "备注"],
            ["1", "空调出风温度", "正常", "18 摄氏度"],
            ["2", "机柜指示灯", "正常", "无告警"],
            ["3", "UPS 电量", "偏低", "已上报"],
            ["4", "消防通道", "畅通", "无堆放"],
        ],
    )
    path = HERE / "扫描件-设备巡检记录.pdf"
    image.save(path, format="PDF", resolution=200.0)
    return path


def png_sample() -> pathlib.Path:
    image = _page_image(
        "会议室使用须知",
        [
            "一、使用前请确认预约时段，不得超时占用。",
            "二、使用结束后请关闭空调、投影仪与照明。",
            "三、桌面与白板由使用人负责清理。",
            "四、设备故障请在系统内报修，不要自行拆装。",
        ],
    )
    path = HERE / "扫描件-会议室使用须知.png"
    image.save(path, format="PNG")
    return path


def jpeg_sample() -> pathlib.Path:
    image = _page_image(
        "应急疏散演练通知",
        [
            "定于 11 月 3 日 15 时开展全员疏散演练。",
            "听到警报后沿就近安全通道撤离，不得使用电梯。",
            "到达集合点后由部门联络人清点人数并上报。",
            "演练期间暂停接待访客，请提前告知来访单位。",
        ],
    )
    path = HERE / "扫描件-应急疏散演练通知.jpg"
    image.save(path, format="JPEG", quality=88)
    return path


def tiff_sample() -> pathlib.Path:
    first = _page_image(
        "图书角借阅规则（第一页）",
        [
            "一、借阅期限为三十天，可续借一次。",
            "二、逾期未还暂停借阅权限，直至归还。",
            "三、工具书与期刊不外借，仅限在图书角查阅。",
        ],
    )
    second = _page_image(
        "图书角借阅规则（第二页）",
        [
            "四、捐贈图书需经行政登记后上架。",
            "五、遗失图书按原价赔偿，绝版图书另议。",
        ],
    )
    path = HERE / "扫描件-图书角借阅规则.tiff"
    # 不压缩会产出 20MB+ 的仓库文件；deflate 无损且体积小一个量级。
    first.save(path, format="TIFF", save_all=True, append_images=[second], compression="tiff_deflate")
    return path


def docx_sample() -> pathlib.Path:
    document = Document()
    style = document.styles["Normal"]
    style.font.name = "微软雅黑"
    style.font.size = Pt(11)

    heading = document.add_heading("内部培训管理办法", level=0)
    heading.alignment = WD_ALIGN_PARAGRAPH.CENTER

    document.add_heading("第一章 报名与考勤", level=1)
    for text in (
        "内部培训由人事统一排期，报名后在系统内确认名额。",
        "报名后无故缺席两次，当季不再接受报名。",
        "培训开始十五分钟后到场视为迟到，迟到三次按缺席一次计。",
    ):
        document.add_paragraph(text, style="List Number")

    document.add_heading("第二章 外部培训", level=1)
    document.add_paragraph("外部培训须提前说明与岗位的关系及费用构成，经批准后参加。")
    document.add_paragraph("培训结束后十个工作日内提交学习小结，未提交的不予报销培训费用。")

    document.add_heading("附表：年度培训计划", level=1)
    table = document.add_table(rows=1, cols=3)
    table.style = "Table Grid"
    header = table.rows[0].cells
    header[0].text, header[1].text, header[2].text = "季度", "主题", "形式"
    for quarter, topic, form in (
        ("第一季度", "信息安全与密码管理", "线上"),
        ("第二季度", "公文写作规范", "线下"),
        ("第三季度", "应急处置演练", "线下"),
        ("第四季度", "年度制度回顾", "线上"),
    ):
        cells = table.add_row().cells
        cells[0].text, cells[1].text, cells[2].text = quarter, topic, form

    path = HERE / "制度-内部培训管理办法.docx"
    document.save(path)
    return path


def xlsx_sample() -> pathlib.Path:
    """两页签的表格：资产清单（含表头、数字与日期）、费用台账（含空行）。"""
    book = Workbook()
    assets = book.active
    assets.title = "资产清单"
    assets.append(["编号", "名称", "类别", "数量", "单价", "入库日期", "保管人"])
    for cell in assets[1]:
        cell.font = Font(bold=True)
    rows = [
        ("ZC-001", "投影仪", "电子设备", 3, 4200.0, "2026-03-12", "行政部"),
        ("ZC-002", "会议室白板", "办公家具", 2, 860.0, "2026-03-12", "行政部"),
        ("ZC-003", "笔记本电脑", "电子设备", 12, 7800.0, "2026-04-01", "信息部"),
        ("ZC-004", "空气净化器", "家电", 5, 1500.0, "2026-04-18", "行政部"),
        ("ZC-005", "文件柜", "办公家具", 8, 640.0, "2026-05-06", "行政部"),
        ("ZC-006", "投影幕布", "电子设备", 3, 320.0, "2026-06-02", "行政部"),
    ]
    for row in rows:
        assets.append(row)
    assets.append([])  # 空行应被跳过
    assets.append(["ZC-007", "便携音箱", "电子设备", 2, 980.0, "2026-06-20", "行政部"])

    ledger = book.create_sheet("费用台账")
    ledger.append(["月份", "科目", "金额", "备注"])
    for cell in ledger[1]:
        cell.font = Font(bold=True)
    for row in [
        ("2026-01", "办公用品", 3200.0, "季度采购"),
        ("2026-02", "设备维修", 1450.0, "投影仪更换灯泡"),
        ("2026-03", "差旅", 8600.0, "华东区域走访"),
        ("2026-04", "培训", 5200.0, "外部讲师"),
    ]:
        ledger.append(row)

    path = HERE / "表格-资产与费用台账.xlsx"
    book.save(path)
    return path


def main() -> int:
    HERE.mkdir(parents=True, exist_ok=True)
    builders = (text_pdf, scanned_pdf, png_sample, jpeg_sample, tiff_sample, docx_sample, xlsx_sample)
    for builder in builders:
        path = builder()
        print(f"{path.name}\t{path.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
