"""效果评测：自制语料 + 标注用例 → 真实链路的检索命中、无证据判定、格式与 locator。

语料与标注见 testdata/corpus/（硬约束 C2：全部自制）。检索当前使用本地确定性
HashEmbedder（字符二元组哈希），本用例是当前链路的效果回归基线，**不代表真实
向量模型的语义检索质量**；接入真实模型后问题集与阈值需重新标定（design §6.1）。
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path
from types import SimpleNamespace

import pytest

from knowledge.adapters.generator import StubGenerator
from knowledge.domain.entities import Actor
from knowledge.repositories import chunks as chunk_repo
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import qa as qa_service
from knowledge.services import retrieval
from knowledge.worker.loop import process_next
from testdata.corpus.labels import (
    CORPUS_FILES,
    DOCX_PARAGRAPHS,
    HIT_CASES,
    NEGATIVE_QUESTIONS,
    PDF_PAGES,
)

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]

CORPUS_ROOT = Path(__file__).resolve().parents[2] / "testdata" / "corpus"
FONT_PATH = Path(os.environ.get("WINDIR", r"C:\Windows")) / "Fonts" / "simhei.ttf"


def _make_docx() -> bytes:
    import io

    import docx

    document = docx.Document()
    for paragraph in DOCX_PARAGRAPHS:
        document.add_paragraph(paragraph)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


def _make_pdf() -> bytes:
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


def _pdf_supported() -> bool:
    try:
        import fpdf  # noqa: F401
    except ImportError:
        return False
    return FONT_PATH.is_file()


@pytest.fixture
def corpus(session, settings, deps):
    """上传全部语料并处理完成；返回 actor / kb / 文档 ID 映射。"""
    actor = Actor(subject_id="eval-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "评测库")
    pdf_ok = _pdf_supported()

    def _upload(filename: str, data: bytes) -> uuid.UUID:
        result = document_service.upload(
            session, actor, settings, deps.storage, deps.registry,
            kb_id=kb.id, filename=filename, data=data,
            embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
        )
        return result.document_id

    ids = {}
    for key, filename in CORPUS_FILES.items():
        ids[key] = _upload(filename, (CORPUS_ROOT / filename).read_bytes())
    ids["backup_docx"] = _upload("数据备份管理细则.docx", _make_docx())
    if pdf_ok:
        ids["visitor_pdf"] = _upload("访客管理规程.pdf", _make_pdf())

    while process_next(session, deps):
        pass

    return SimpleNamespace(actor=actor, kb=kb, ids=ids, pdf_ok=pdf_ok)


def test_hit_cases_and_metrics(session, corpus, settings, deps):
    """标注命中用例：top10 内应命中预期片段，且命中分块属于预期文档。"""
    cases = [case for case in HIT_CASES if case.get("kind") != "pdf" or corpus.pdf_ok]

    misses: list[str] = []
    hit1 = 0
    hit5 = 0
    for case in cases:
        hits = retrieval.search(
            session, corpus.actor, settings, deps.embedder,
            query=str(case["question"]), kb_ids=[corpus.kb.id], mode="hybrid", top_k=10,
        )
        snippets = [str(snippet) for snippet in case["any_of"]]
        matched = [
            hit for hit in hits if any(snippet in hit.text for snippet in snippets)
        ]
        if not matched:
            misses.append(f"{case['question']} → 未在 top10 命中 {snippets}")
            continue
        rank = hits.index(matched[0]) + 1
        hit1 += rank == 1
        hit5 += rank <= 5
        assert str(matched[0].document_id) == str(corpus.ids[str(case["doc"])]), (
            f"{case['question']} 命中了错误文档：{matched[0].document_id}"
        )

    assert not misses, "未命中用例：\n" + "\n".join(misses)

    rate1 = hit1 / len(cases)
    rate5 = hit5 / len(cases)
    print(f"\n[eval] 命中用例 {len(cases)} 条：top10 全命中；"
          f"hit@5 = {rate5:.0%}，hit@1 = {rate1:.0%}")
    # 主题相近条款会挤占排名（trigram + 哈希向量的已知局限），hit@1 允许低于 top 召回
    assert rate5 >= 0.8, f"hit@5 过低：{rate5:.0%}"
    assert rate1 >= 0.6, f"首位命中率过低：{rate1:.0%}"


def test_negative_questions_return_no_evidence(session, corpus, settings, deps):
    """语料外问题：检索 0 命中，问答返回 no_evidence 且不产生引用。"""
    for question in NEGATIVE_QUESTIONS:
        hits = retrieval.search(
            session, corpus.actor, settings, deps.embedder,
            query=question, kb_ids=[corpus.kb.id],
        )
        assert hits == [], f"{question} 不应命中"

        answer = qa_service.answer(
            session, corpus.actor, settings, deps.embedder, StubGenerator(),
            question=question, kb_ids=[corpus.kb.id],
        )
        assert answer.status == "no_evidence"
        assert answer.citations == []
    print(f"\n[eval] 负面用例 {len(NEGATIVE_QUESTIONS)} 条全部 no_evidence")


def test_docx_chunks_locate_by_paragraph(session, corpus, settings, deps):
    hits = retrieval.search(
        session, corpus.actor, settings, deps.embedder,
        query="核心业务数据多久执行一次全量备份", kb_ids=[corpus.kb.id],
    )
    assert hits, "DOCX 内容应可检索"
    hit = hits[0]
    assert "每日凌晨" in hit.text

    chunk = chunk_repo.get_owned(session, corpus.actor, uuid.UUID(hit.chunk_id))
    assert chunk["locator"]["type"] == "docx"
    assert chunk["locator"].get("page") is None
    assert chunk["locator"].get("paragraph") is not None


def test_pdf_chunks_locate_by_page(session, corpus, settings, deps):
    if not corpus.pdf_ok:
        pytest.skip("fpdf2 或中文字体不可用，跳过 PDF 评测")

    expectations = [
        ("访客进入办公区需要谁陪同", 1),
        ("访客证的有效期是多久", 2),
        ("外来车辆应该停放在哪里", 2),
        ("消防通道可以堆放物品吗", 3),
        ("末班班车几点发车", 4),
        ("监控录像保存多长时间", 5),
    ]
    for question, expected_page in expectations:
        hits = retrieval.search(
            session, corpus.actor, settings, deps.embedder,
            query=question, kb_ids=[corpus.kb.id],
        )
        assert hits, f"PDF 内容应可检索：{question}"
        assert hits[0].locator["type"] == "pdf"
        assert hits[0].locator["page"] == expected_page, (
            f"{question} 应位于第 {expected_page} 页，实际 {hits[0].locator['page']}"
        )


def test_md_locator_offsets_match_source(session, corpus, settings, deps):
    """MD 的 char 级 locator 应能按偏移切出分块原文（引用可回溯到字符）。"""
    answer = qa_service.answer(
        session, corpus.actor, settings, deps.embedder, StubGenerator(),
        question="会议室使用后需要完成哪些事项", kb_ids=[corpus.kb.id],
    )
    assert answer.status == "answered"
    citation = answer.citations[0]
    assert "清理现场" in citation.excerpt

    chunk = chunk_repo.get_owned(session, corpus.actor, uuid.UUID(citation.chunk_id))
    source = (CORPUS_ROOT / CORPUS_FILES["meeting"]).read_text(encoding="utf-8")
    start = chunk["locator"]["char_start"]
    end = chunk["locator"]["char_end"]
    assert source[start:end].strip() == chunk["text"].strip(), "locator 偏移与原文不一致"


def test_kb_scope_isolation(session, settings, deps):
    """检索范围不超出给定知识库（design §6.1：范围由请求显式声明）。"""
    actor = Actor(subject_id="scope-user", tenant_id="default")
    kb_meeting = knowledge_service.create(session, actor, "行政库")
    kb_security = knowledge_service.create(session, actor, "信息库")

    for kb, key in ((kb_meeting, "meeting"), (kb_security, "security")):
        filename = CORPUS_FILES[key]
        document_service.upload(
            session, actor, settings, deps.storage, deps.registry,
            kb_id=kb.id, filename=filename, data=(CORPUS_ROOT / filename).read_bytes(),
            embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
        )
    while process_next(session, deps):
        pass

    in_scope = retrieval.search(
        session, actor, settings, deps.embedder,
        query="借用会议室需要提前多久登记", kb_ids=[kb_meeting.id],
    )
    assert in_scope and "提前一天" in in_scope[0].text

    out_scope = retrieval.search(
        session, actor, settings, deps.embedder,
        query="借用会议室需要提前多久登记", kb_ids=[kb_security.id],
    )
    assert out_scope == []
