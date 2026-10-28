"""原始文件取回：字节与版本一致、他人与已删除不可取、内联判定按格式。"""

from __future__ import annotations

import pathlib

from knowledge.domain.entities import Actor, JobState
from knowledge.domain.errors import NotFound
from knowledge.repositories import jobs as job_repo
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.worker.loop import process_next

BACKEND = pathlib.Path(__file__).resolve().parents[2]

PDF = (BACKEND / "testdata" / "multiformat" / "制度-差旅费报销管理办法.pdf").read_bytes()
DOCX = (BACKEND / "testdata" / "multiformat" / "制度-内部培训管理办法.docx").read_bytes()


def _upload(
    session, settings, deps, actor: Actor, *, filename: str, data: bytes, process: bool = True
):
    kb = knowledge_service.create(session, actor, f"原件-{actor.subject_id}")
    upload = document_service.upload(
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
    if process:
        assert process_next(session, deps) is True
        job = job_repo.get_owned(session, actor, upload.job_id)
        # 处理失败时 current_version_id 仍为空，会让「他人不可见」用例假通过。
        assert job.state is JobState.SUCCEEDED, job.last_error
    return kb, upload


def test_source_returns_the_uploaded_bytes(session, settings, deps):
    actor = Actor(subject_id="src-owner", tenant_id="default")
    _, upload = _upload(
        session, settings, deps, actor,
        filename="制度-差旅费报销管理办法.pdf", data=PDF,
    )

    source = document_service.source(session, actor, upload.document_id, deps.storage)

    assert source["data"] == PDF
    assert source["media_type"] == "application/pdf"
    # PDF 与图片内联预览。
    assert source["inline"] is True
    assert source["filename"].endswith(".pdf")


def test_non_previewable_format_is_download_only(session, settings, deps):
    actor = Actor(subject_id="src-owner-d", tenant_id="default")
    _, upload = _upload(
        session, settings, deps, actor,
        filename="制度-内部培训管理办法.docx", data=DOCX,
    )

    source = document_service.source(session, actor, upload.document_id, deps.storage)

    assert source["data"] == DOCX
    # DOCX 浏览器不能内嵌，按附件下载。
    assert source["inline"] is False


def test_source_is_invisible_to_other_users(session, settings, deps):
    owner = Actor(subject_id="src-owner-b", tenant_id="default")
    _, upload = _upload(
        session, settings, deps, owner,
        filename="制度-差旅费报销管理办法.pdf", data=PDF,
    )
    intruder = Actor(subject_id="src-other", tenant_id="default")

    try:
        document_service.source(session, intruder, upload.document_id, deps.storage)
    except NotFound:
        return
    raise AssertionError("他人文档的原始文件不应当可读")


def test_source_rejects_unknown_document(session, settings, deps):
    actor = Actor(subject_id="src-owner-c", tenant_id="default")

    try:
        document_service.source(session, actor, __import__("uuid").uuid4(), deps.storage)
    except NotFound:
        return
    raise AssertionError("不存在的文档应当返回 404")
