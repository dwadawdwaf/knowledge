"""检索工程回归：验证拒答、引用定位与知识库范围，不统计效果指标。"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select

from knowledge.adapters.generator import StubGenerator
from knowledge.domain.entities import Actor
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories.tables import Chunk
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import qa as qa_service
from knowledge.services import retrieval
from knowledge.worker.loop import process_next
from testdata.engineering.retrieval_documents import make_docx, make_pdf, pdf_supported

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]

FIXTURE_ROOT = Path(__file__).resolve().parents[2] / "testdata" / "corpus"
MEETING_FIXTURE = "行政-会议室管理办法.md"
SECURITY_FIXTURE = "信息-账号与密码管理规范.md"


@pytest.fixture
def indexed_document(session, settings, deps):
    """每个用例只索引验证契约所需的文件。"""
    actor = Actor(subject_id="retrieval-contract-user", tenant_id="default")
    kb = knowledge_service.create(session, actor, "检索回归库")

    def upload(filename: str, data: bytes) -> None:
        document_service.upload(
            session, actor, settings, deps.storage, deps.registry,
            kb_id=kb.id, filename=filename, data=data,
            embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
        )
        assert process_next(session, deps)

    return actor, kb.id, upload


def test_docx_chunks_locate_by_paragraph(session, indexed_document, settings, deps):
    actor, kb_id, upload = indexed_document
    upload("backup.docx", make_docx())
    hits = retrieval.search(
        session, actor, settings, deps.embedder,
        query="核心业务数据多久执行一次全量备份", kb_ids=[kb_id],
    )
    assert hits, "DOCX 内容应可检索"
    hit = hits[0]
    assert "每日凌晨" in hit.text

    chunk = chunk_repo.get_owned(session, actor, uuid.UUID(hit.chunk_id))
    assert chunk["locator"]["type"] == "docx"
    assert chunk["locator"].get("page") is None
    assert chunk["locator"].get("paragraph") is not None


def test_pdf_chunks_locate_by_page(session, indexed_document, settings, deps, query_vector):
    if not pdf_supported():
        pytest.skip("fpdf2 或中文测试字体不可用")
    actor, kb_id, upload = indexed_document
    upload("visitor.pdf", make_pdf())

    expectations = [
        ("访客进入办公区需要谁陪同", 1, "全程陪同"),
        ("访客证的有效期是多久", 2, "当日有效"),
        ("外来车辆应该停放在哪里", 2, "访客专区"),
        ("消防通道可以堆放物品吗", 3, "不得堆放"),
        ("末班班车几点发车", 4, "晚上十点半"),
        ("监控录像保存多长时间", 5, "保存九十天"),
    ]
    rows = session.scalars(select(Chunk)).all()
    for question, expected_page, phrase in expectations:
        expected = next(row for row in rows if phrase in row.text and row.locator.get("page") == expected_page)
        query_vector(question, expected.embedding)
        hits = retrieval.search(
            session, actor, settings, deps.embedder,
            query=question, kb_ids=[kb_id],
        )
        assert hits, f"PDF 内容应可检索：{question}"
        assert hits[0].locator["type"] == "pdf"
        assert phrase in hits[0].text
        assert hits[0].locator["page"] == expected_page, (
            f"{question} 应位于第 {expected_page} 页，实际 {hits[0].locator['page']}"
        )


def test_md_locator_offsets_match_source(session, indexed_document, settings, deps, query_vector):
    """MD 的 char 级 locator 应能按偏移切出分块原文（引用可回溯到字符）。"""
    actor, kb_id, upload = indexed_document
    data = (FIXTURE_ROOT / MEETING_FIXTURE).read_bytes()
    upload("meeting.md", data)
    expected = session.scalar(select(Chunk).where(Chunk.text.contains("清理现场")))
    query_vector("会议室使用后需要完成哪些事项", expected.embedding)
    answer = qa_service.answer(
        session, actor, settings, deps.embedder, StubGenerator(),
        question="会议室使用后需要完成哪些事项", kb_ids=[kb_id],
    )
    assert answer.status == "answered"
    citation = answer.citations[0]
    assert "清理现场" in citation.excerpt

    chunk = chunk_repo.get_owned(session, actor, uuid.UUID(citation.chunk_id))
    source = data.decode("utf-8")
    for item in chunk["locator"]["sources"]:
        loc = item["locator"]
        assert source[loc["char_start"]:loc["char_end"]] == chunk["text"][item["chunk_start"]:item["chunk_end"]]


def test_kb_scope_isolation(session, settings, deps):
    """检索范围不超出给定知识库（design §6.1：范围由请求显式声明）。"""
    actor = Actor(subject_id="scope-user", tenant_id="default")
    kb_meeting = knowledge_service.create(session, actor, "行政库")
    kb_security = knowledge_service.create(session, actor, "信息库")

    for kb, filename in ((kb_meeting, MEETING_FIXTURE), (kb_security, SECURITY_FIXTURE)):
        document_service.upload(
            session, actor, settings, deps.storage, deps.registry,
            kb_id=kb.id, filename=filename, data=(FIXTURE_ROOT / filename).read_bytes(),
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


def test_keyword_finds_short_literal_in_long_chunk(session, indexed_document, settings, deps):
    actor, kb_id, upload = indexed_document
    body = "".join(chr(0x5200 + offset) for offset in range(320))
    upload("literal.txt", f"{body} 氟泵在二层，设备编号 RX-830。".encode())
    for query, expected in (("氟泵", "氟泵"), ("rx-830", "RX-830")):
        hits = retrieval.search(
            session, actor, settings, deps.embedder, query=query, kb_ids=[kb_id], mode="keyword",
        )
        assert hits and expected in hits[0].text
    assert not retrieval.search(
        session, actor, settings, deps.embedder, query="ZZ-674",
        kb_ids=[kb_id], mode="keyword",
    )
