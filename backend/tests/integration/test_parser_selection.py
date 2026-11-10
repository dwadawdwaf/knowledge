"""选择、冻结、去重与发布回归；本机服务使用合成协议夹具，不加载解析模型。"""

import io
import json
import os
import threading
import zipfile
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fastapi.testclient import TestClient

from knowledge.adapters.generator import StubGenerator
from knowledge.adapters.parser import build_parser_registry
from knowledge.api.deps import (
    get_actor,
    get_embedder,
    get_registry,
    get_session,
    get_settings_dep,
    get_storage,
)
from knowledge.domain.entities import Actor
from knowledge.domain.errors import NotFound
from knowledge.entrypoints.api import create_app
from knowledge.repositories.tables import IndexBuild
from knowledge.services import document, knowledge, qa, retrieval
from knowledge.worker.loop import process_next

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated DB required"),
]


@contextmanager
def parser_server():
    state = {"fail": False, "calls": 0, "nul": False, "blocks": None, "word_image": False}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            state["calls"] += 1
            self.send_response(503 if state["fail"] else 200)
            self.end_headers()
            if state["fail"]:
                return
            block = "合成设备保修期限为18个月。"
            if state["nul"]:
                block += "备注甲\x00乙"
            blocks = state["blocks"] or [{"type": "text", "content": block}]
            image = None
            if state["word_image"]:
                if b'filename="input.png"' in body:
                    blocks = [
                        {"type": "text", "content": "图中校准编号7842"},
                        {"type": "text", "content": "编号每周复核。"},
                    ]
                else:
                    image = "images/page_0_image_0.png"
                    blocks = [{"type": "image", "content": "", "image_source": image}]
            stream = io.BytesIO()
            with zipfile.ZipFile(stream, "w") as archive:
                if image:
                    archive.writestr(image, b"\x89PNG\r\n\x1a\nsynthetic")
                archive.writestr(
                    "structured_content.json",
                    json.dumps(
                        {
                            "metadata": {"producer": {"name": "mineru", "version": "4.1.0"}},
                            "is_full_document": True,
                            "pages": [{"page_idx": 0, "blocks": blocks}],
                        }
                    ),
                )
            self.wfile.write(stream.getvalue())

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", state
    finally:
        server.shutdown()
        server.server_close()
        thread.join()


def test_selected_parser_publish_dedup_rebuild_failure_and_authorization(
    session, actor, settings, deps
):
    profile = "mineru-api4.1-v1"
    with parser_server() as (url, state):
        settings = settings.model_copy(update={"mineru_url": url})
        registry = build_parser_registry(settings)
        deps = replace(deps, settings=settings, registry=registry)
        kb = knowledge.create(session, actor, "合成解析选择")
        kwargs = {
            "filename": "synthetic.csv",
            "data": "说明,月数\n合成设备,18\n".encode(),
            "embedder_model": deps.embedder.model,
            "embedder_dim": 1024,
        }
        result = document.upload(
            session,
            actor,
            settings,
            deps.storage,
            registry,
            kb_id=kb.id,
            parser_profile=profile,
            **kwargs,
        )
        assert result.parser_profile == profile
        assert document.detail(session, actor, result.document_id)["parser_profile"] == profile
        assert process_next(session, deps)
        session.expire_all()
        info = document.detail(session, actor, result.document_id)
        assert info["parser_profile"] == profile
        build = session.get(IndexBuild, result.build_id)
        frozen = dict(build.chunk_config)
        answer = qa.answer(
            session,
            actor,
            settings,
            deps.embedder,
            StubGenerator(),
            question="合成设备保修期限",
            kb_ids=[kb.id],
        )
        assert answer.status == "answered" and answer.citations
        assert (
            answer.citations[0].locator["type"] == "csv"
            and "page" not in answer.citations[0].locator
        )
        duplicate = document.upload(
            session,
            actor,
            settings,
            deps.storage,
            registry,
            kb_id=kb.id,
            parser_profile="legacy-v1",
            **kwargs,
        )
        assert duplicate.deduplicated and duplicate.parser_profile == profile
        assert state["calls"] == 1
        rebuilt = document.reindex(
            session,
            actor,
            settings,
            document_id=result.document_id,
            version_id=result.version_id,
            expected_generation=info["generation"],
            embedder_model=deps.embedder.model,
            embedder_dim=1024,
        )
        assert session.get(IndexBuild, rebuilt.build_id).chunk_config == frozen
        assert process_next(session, deps)
        session.expire_all()
        assert session.get(IndexBuild, result.build_id).chunk_config == frozen
        previous = document.detail(session, actor, result.document_id)
        state["fail"] = True
        failed = document.add_version(
            session,
            actor,
            settings,
            deps.storage,
            registry,
            document_id=result.document_id,
            expected_generation=previous["generation"],
            parser_profile=profile,
            **{**kwargs, "data": b"synthetic,19\n"},
        )
        assert failed.parser_profile == profile and process_next(session, deps)
        session.expire_all()
        assert (
            document.detail(session, actor, result.document_id)["active_build_id"]
            == previous["active_build_id"]
        )
        stranger = Actor(subject_id="other", tenant_id=actor.tenant_id)
        with pytest.raises(NotFound):
            document.detail(session, stranger, result.document_id)
        with pytest.raises(NotFound):
            document.source(session, stranger, result.document_id, deps.storage)
        document.delete(session, actor, result.document_id, settings.max_attempts)
        with pytest.raises(NotFound):
            document.detail(session, actor, result.document_id)
        with pytest.raises(NotFound):
            document.source(session, actor, result.document_id, deps.storage)
        with pytest.raises(NotFound):
            document.chunks(session, actor, result.document_id)


def test_mineru_nul_publishes_with_warning_without_changing_original(
    session, actor, settings, deps
):
    with parser_server() as (url, state):
        state["nul"] = True
        settings = settings.model_copy(update={"mineru_url": url})
        registry = build_parser_registry(settings)
        deps = replace(deps, settings=settings, registry=registry)
        kb = knowledge.create(session, actor, "合成无效字符")
        original = b"%PDF-synthetic-nul-output"
        result = document.upload(
            session,
            actor,
            settings,
            deps.storage,
            registry,
            kb_id=kb.id,
            filename="synthetic.pdf",
            data=original,
            parser_profile="mineru-api4.1-v1",
            embedder_model=deps.embedder.model,
            embedder_dim=1024,
        )
        assert process_next(session, deps)
        session.expire_all()
        blocks = document.chunks(session, actor, result.document_id)
        assert "\x00" not in blocks["items"][0]["text"]
        assert "备注甲\ufffd乙" in blocks["items"][0]["text"]
        assert "mineru_nul_replaced" in session.get(IndexBuild, result.build_id).warnings
        assert document.source(session, actor, result.document_id, deps.storage)["data"] == original
        stranger = Actor(subject_id="other", tenant_id=actor.tenant_id)
        with pytest.raises(NotFound):
            document.chunks(session, stranger, result.document_id)
        document.delete(session, actor, result.document_id, settings.max_attempts)
        with pytest.raises(NotFound):
            document.chunks(session, actor, result.document_id)


def test_upload_form_optional_profile_is_strict_and_returned(session, actor, settings, deps):
    app = create_app()

    def fixed(value):
        def dependency():
            return value

        return dependency

    for dependency, value in [
        (get_actor, actor),
        (get_session, session),
        (get_settings_dep, settings),
        (get_embedder, deps.embedder),
        (get_registry, deps.registry),
        (get_storage, deps.storage),
    ]:
        app.dependency_overrides[dependency] = fixed(value)
    client = TestClient(app)
    kb = knowledge.create(session, actor, "合成接口")
    path = f"/api/v1/knowledge-bases/{kb.id}/documents"
    response = client.post(
        path,
        files={"file": ("synthetic.txt", b"synthetic")},
        data={"parser_profile": "mineru-api4.1-v1"},
    )
    assert response.status_code == 202
    assert response.json()["data"]["parser_profile"] == "mineru-api4.1-v1"
    response = client.post(
        path, files={"file": ("synthetic.txt", b"new")}, data={"parser_profile": "unknown"}
    )
    assert response.status_code == 422
    assert client.get("/openapi.json").status_code == 200


def test_pdf_default_is_frozen_for_upload_and_new_version(session, actor, settings, deps):
    with parser_server() as (url, state):
        settings = settings.model_copy(
            update={"pdf_parser_profile": "mineru-api4.1-v1", "mineru_url": url}
        )
        registry = build_parser_registry(settings)
        deps = replace(deps, settings=settings, registry=registry)
        kb = knowledge.create(session, actor, "合成PDF默认")
        common = {
            "filename": "synthetic.pdf",
            "embedder_model": deps.embedder.model,
            "embedder_dim": 1024,
        }
        result = document.upload(
            session,
            actor,
            settings,
            deps.storage,
            registry,
            kb_id=kb.id,
            data=b"%PDF-1.7 synthetic v1",
            **common,
        )
        assert result.parser_profile == "mineru-api4.1-v1"
        assert process_next(session, deps)
        session.expire_all()
        info = document.detail(session, actor, result.document_id)
        assert info["parser_profile"] == "mineru-api4.1-v1"
        updated = document.add_version(
            session,
            actor,
            settings,
            deps.storage,
            registry,
            document_id=result.document_id,
            expected_generation=info["generation"],
            data=b"%PDF-1.7 synthetic v2",
            **common,
        )
        assert updated.parser_profile == "mineru-api4.1-v1"
        assert process_next(session, deps)
        session.expire_all()
        assert state["calls"] == 2
        assert (
            session.get(IndexBuild, result.build_id).chunk_config["parser_profile"]
            == "mineru-api4.1-v1"
        )


def test_article_keyword_hit_includes_subpoints(session, actor, settings, deps):
    lines = [
        "第八条 样品登记", "（一）低温样品贴蓝色标签。", "（二）常温样品放入干燥柜。",
        "（三）危险样品由管理员签收。", "（四）空容器单独登记。", "第九条 设备保养", "每月校准。",
    ]
    with parser_server() as (url, state):
        state["blocks"] = [{"type": "text", "content": line} for line in lines]
        settings = settings.model_copy(update={
            "mineru_url": url, "retrieval_profile": "optimized-v2",
        })
        registry = build_parser_registry(settings)
        deps = replace(deps, settings=settings, registry=registry)
        kb = knowledge.create(session, actor, "合成条款")
        uploaded = document.upload(
            session, actor, settings, deps.storage, registry, kb_id=kb.id,
            filename="synthetic.pdf", data=b"%PDF-synthetic-article", parser_profile="mineru-api4.1-v1",
            embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
        )
        assert process_next(session, deps)
        session.expire_all()
        hits = retrieval.search(
            session, actor, settings, deps.embedder, query="第八条相关内容", kb_ids=[kb.id],
            mode="keyword", top_k=1,
        )
        assert len(hits) == 1 and hits[0].text.splitlines() == lines[:5]
        assert hits[0].locator["precision"] == "composite"
        stranger = Actor(subject_id="other", tenant_id=actor.tenant_id)
        with pytest.raises(NotFound):
            document.chunks(session, stranger, uploaded.document_id)
        document.delete(session, actor, uploaded.document_id, settings.max_attempts)
        assert retrieval.search(
            session, actor, settings, deps.embedder, query="第八条相关内容", kb_ids=[kb.id], mode="keyword",
        ) == []


def test_word_image_text_reaches_published_search_index(session, actor, settings, deps):
    with parser_server() as (url, state):
        state["word_image"] = True
        settings = settings.model_copy(update={"mineru_url": url, "document_parser_profile": "legacy-v1"})
        registry = build_parser_registry(settings)
        deps = replace(deps, settings=settings, registry=registry)
        kb = knowledge.create(session, actor, "合成 Word 图片")
        header = b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1" + bytes(18) + b"\x03\x00\xfe\xff\x09\x00"
        original = header.ljust(1024, b"\x00")
        uploaded = document.upload(
            session, actor, settings, deps.storage, registry, kb_id=kb.id,
            filename="synthetic.doc", data=original,
            embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
        )
        assert uploaded.parser_profile == "mineru-api4.1-v1"
        assert process_next(session, deps)
        session.expire_all()
        hits = retrieval.search(
            session, actor, settings, deps.embedder, query="图中校准编号7842", kb_ids=[kb.id], mode="keyword",
        )
        assert state["calls"] == 2
        assert len(hits) == 1 and hits[0].text == "图中校准编号7842\n编号每周复核。"
        assert hits[0].locator["type"] == "doc" and "page" not in hits[0].locator
        assert hits[0].locator["precision"] == "composite"
        assert len(hits[0].locator["sources"]) == 2
        assert "word_image_ocr_requires_review" in session.get(IndexBuild, uploaded.build_id).warnings
        assert document.source(session, actor, uploaded.document_id, deps.storage)["data"] == original
        document.delete(session, actor, uploaded.document_id, settings.max_attempts)
        assert retrieval.search(
            session, actor, settings, deps.embedder, query="图中校准编号7842", kb_ids=[kb.id], mode="keyword",
        ) == []


def test_new_import_uses_evidence_units_and_dedup_does_not_enqueue(session, actor, settings, deps):
    original = b"%PDF-synthetic-workflow"
    heading, *body = ["试验准备", "在平整台面上固定装置。", "启动前确认以下事项：", "- 打开排气阀。", "- 记录环境温度。"]
    with parser_server() as (url, state):
        state["blocks"] = [
            {"type": "title", "content": heading, "level": 2},
            *[{"type": "text", "content": line} for line in body],
        ]
        settings = settings.model_copy(update={"mineru_url": url})
        registry = build_parser_registry(settings)
        deps = replace(deps, settings=settings, registry=registry)

        kb = knowledge.create(session, actor, "文档单元分块")

        def upload():
            return document.upload(
                session, actor, settings, deps.storage, registry, kb_id=kb.id,
                filename="synthetic.pdf", data=original, parser_profile="mineru-api4.1-v1",
                embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
            )
        new = upload()
        assert process_next(session, deps)
        session.expire_all()
        new_items = document.chunks(session, actor, new.document_id)["items"]
        assert [item["text"] for item in new_items] == ["\n".join([heading, *body])]
        assert new_items[0]["locator"]["precision"] == "composite"
        assert session.get(IndexBuild, new.build_id).chunk_config["strategy"] == "evidence-unit-v1"
        duplicate = upload()
        assert duplicate.deduplicated and duplicate.build_id == new.build_id
        assert not process_next(session, deps)
