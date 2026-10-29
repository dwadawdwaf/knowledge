"""格式多样性测试：表格内容、加密与空白 PDF、格式白名单边界。

覆盖此前缺口：DOCX 表格内容不得静默丢弃（硬约束 C8）、加密 PDF 须映射为
确定性错误 PASSWORD_PROTECTED（runbook §3）、白名单外格式 415 拒收。
"""

from __future__ import annotations

import io
import os

import pytest

from knowledge.domain.entities import Actor, JobState
from knowledge.domain.errors import UnsupportedFormat
from knowledge.repositories import jobs as job_repo
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import retrieval
from knowledge.worker.loop import process_next

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


def _upload_processed(session, actor, settings, deps, kb, filename: str, data: bytes):
    result = document_service.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename=filename,
        data=data,
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    while process_next(session, deps):
        pass
    return result


def _make_docx_with_table() -> bytes:
    import docx

    document = docx.Document()
    document.add_paragraph("《IT 资产登记管理办法（测试资料）》")
    document.add_paragraph("第一条 各部门设备领用须在登记表内如实填写。")
    table = document.add_table(rows=4, cols=3)
    rows = [
        ("部门", "设备类型", "负责人"),
        ("网络部", "核心交换机", "陈建工"),
        ("财务部", "凭证打印机", "刘会敏"),
        ("行政部", "会议室投影仪", "赵后勤"),
    ]
    for row, values in zip(table.rows, rows):
        for cell, value in zip(row.cells, values):
            cell.text = value
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def test_docx_table_cells_are_indexed(session, settings, deps):
    actor = Actor(subject_id="fmt-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "格式库")
    result = _upload_processed(
        session, actor, settings, deps, kb, "资产登记.docx", _make_docx_with_table()
    )

    job = job_repo.get_owned(session, actor, result.job_id)
    assert job.state is JobState.SUCCEEDED

    # 表格单元格内容可检索（此前被静默丢弃）
    for keyword, expected in [("凭证打印机 刘会敏", "刘会敏"), ("核心交换机 负责人", "陈建工")]:
        hits = retrieval.search(
            session,
            actor,
            settings,
            deps.embedder,
            query=keyword,
            kb_ids=[kb.id],
        )
        assert hits, f"表格内容应可检索：{keyword}"
        assert expected in hits[0].text

    # 表格块的 locator 指明表格序号
    hits = retrieval.search(
        session,
        actor,
        settings,
        deps.embedder,
        query="凭证打印机",
        kb_ids=[kb.id],
    )
    assert hits[0].locator["type"] == "docx"
    assert hits[0].locator.get("block") == 1

    # 原文视图的结构化预览：段落与表格都返回，不依赖分块
    preview = document_service.preview_docx(
        session, actor, result.document_id, deps.storage
    )
    assert preview["kind"] == "docx"
    assert any(block["type"] == "table" for block in preview["blocks"])
    assert any("凭证打印机" in cell for block in preview["blocks"]
               if block["type"] == "table" for row in block["rows"] for cell in row)


def _make_pdf_with_table() -> bytes:
    from fpdf import FPDF

    font_path = os.path.join(os.environ.get("WINDIR", r"C:\Windows"), "Fonts", "simhei.ttf")
    pdf = FPDF()
    pdf.add_font("hei", "", font_path)
    pdf.add_font("hei", "B", font_path)  # SimHei 无粗体变体，表头加粗复用同文件
    pdf.set_font("hei", size=11)
    pdf.add_page()
    pdf.multi_cell(0, 8, "《部门报销汇总表（测试资料）》", new_x="LMARGIN", new_y="NEXT")
    with pdf.table() as table:
        for row in (
            ("月份", "事项", "金额"),
            ("三月", "展会物料", "1888.50 元"),
            ("四月", "差旅住宿", "2350.00 元"),
        ):
            pdf_row = table.row()
            for cell in row:
                pdf_row.cell(cell)
    return bytes(pdf.output())


def test_pdf_table_text_is_retrievable(session, settings, deps):
    actor = Actor(subject_id="fmt-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "PDF 表格库")
    result = _upload_processed(
        session, actor, settings, deps, kb, "报销汇总.pdf", _make_pdf_with_table()
    )
    assert job_repo.get_owned(session, actor, result.job_id).state is JobState.SUCCEEDED

    # PDF 表格以文本流提取（顺序可能交错），数字与专名应可检索
    hits = retrieval.search(
        session,
        actor,
        settings,
        deps.embedder,
        query="展会物料 报销金额",
        kb_ids=[kb.id],
    )
    assert hits and "1888.50" in hits[0].text


def _make_encrypted_pdf() -> bytes:
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_page(writer.add_blank_page(width=300, height=200))
    writer.encrypt("secret-pass")
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def test_encrypted_pdf_fails_with_password_protected(session, settings, deps):
    actor = Actor(subject_id="fmt-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "加密库")
    result = _upload_processed(
        session, actor, settings, deps, kb, "加密文件.pdf", _make_encrypted_pdf()
    )

    job = job_repo.get_owned(session, actor, result.job_id)
    assert job.state is JobState.FAILED
    assert job.last_error["code"] == "PASSWORD_PROTECTED"
    assert job.attempt == 1, "确定性错误不应重试"


def test_blank_pdf_fails_with_empty_content(session, settings, deps):
    from fpdf import FPDF

    pdf = FPDF()
    pdf.add_page()
    actor = Actor(subject_id="fmt-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "空白库")
    result = _upload_processed(session, actor, settings, deps, kb, "空白.pdf", bytes(pdf.output()))

    job = job_repo.get_owned(session, actor, result.job_id)
    assert job.state is JobState.FAILED
    assert job.last_error["code"] == "EMPTY_CONTENT"


@pytest.mark.parametrize(
    "filename,data",
    [
        ("旧表.xls", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32),
        ("旧文档.doc", b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + b"\x00" * 32),
        ("扫描件.jpg", b"\xff\xd8\xff\xe0" + b"\x00" * 32),
    ],
    ids=["xls", "doc", "jpg"],
)
def test_non_whitelisted_formats_rejected(session, settings, deps, filename, data):
    actor = Actor(subject_id="fmt-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "白名单库")
    with pytest.raises(UnsupportedFormat):
        document_service.upload(
            session,
            actor,
            settings,
            deps.storage,
            deps.registry,
            kb_id=kb.id,
            filename=filename,
            data=data,
            embedder_model=deps.embedder.model,
            embedder_dim=deps.embedder.dimension,
        )


def _xlsx_bytes() -> bytes:
    from io import BytesIO

    from openpyxl import Workbook

    book = Workbook()
    sheet = book.active
    sheet.title = "人员名单"
    sheet.append(["部门", "负责人", "工位"])
    sheet.append(["网络部", "陈建工", "B-201"])
    sheet.append(["行政部", "李静", "A-102"])
    stream = BytesIO()
    book.save(stream)
    return stream.getvalue()


def test_xlsx_is_indexed_with_sheet_and_row_locators(session, settings, deps):
    actor = Actor(subject_id="fmt-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "表格库")
    result = _upload_processed(
        session, actor, settings, deps, kb, "人员名单.xlsx", _xlsx_bytes()
    )

    job = job_repo.get_owned(session, actor, result.job_id)
    assert job.state is JobState.SUCCEEDED, job.last_error

    view = document_service.chunks(session, actor, result.document_id)
    assert view["total"] == 1
    item = view["items"][0]
    assert item["locator"]["sheet"] == "人员名单"
    assert (item["locator"]["row_start"], item["locator"]["row_end"]) == (1, 3)
    assert item["locator"]["precision"] == "sheet"
    # 单元格内容必须可检索，不能只留表名
    assert "陈建工" in item["text"] and "B-201" in item["text"]


def _pptx_bytes() -> bytes:
    import zipfile

    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        xml = (
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<p:sld xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
            'xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">'
            "<p:cSld><p:spTree>"
            "<a:p><a:r><a:t>季度汇报（测试资料）</a:t></a:r></a:p>"
            "<a:p><a:r><a:t>第一条 本季度完成事项。</a:t></a:r></a:p>"
            "</p:spTree></p:cSld></p:sld>"
        )
        archive.writestr("ppt/slides/slide1.xml", xml)
    return buffer.getvalue()


def test_csv_and_pptx_are_indexed_with_their_locators(session, settings, deps):
    actor = Actor(subject_id="fmt-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "办公格式库")
    csv_result = _upload_processed(
        session,
        actor,
        settings,
        deps,
        kb,
        "人员名单.csv",
        "部门,负责人,工位\n网络部,陈建工,B-201\n行政部,李静,A-102\n".encode(),
    )
    pptx_result = _upload_processed(
        session, actor, settings, deps, kb, "季度汇报.pptx", _pptx_bytes()
    )

    csv_view = document_service.chunks(session, actor, csv_result.document_id)
    assert csv_view["total"] == 1
    csv_item = csv_view["items"][0]
    assert csv_item["locator"]["type"] == "csv"
    assert (csv_item["locator"]["row_start"], csv_item["locator"]["row_end"]) == (1, 3)
    assert "陈建工" in csv_item["text"]

    pptx_view = document_service.chunks(session, actor, pptx_result.document_id)
    assert pptx_view["total"] == 1
    pptx_item = pptx_view["items"][0]
    assert pptx_item["locator"]["type"] == "pptx"
    assert pptx_item["locator"]["page"] == 1
    assert "季度汇报" in pptx_item["text"]
