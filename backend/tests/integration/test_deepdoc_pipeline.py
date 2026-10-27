"""真实数据库 + 本机 HTTP 协议夹具；不表示真实 DeepDoc OCR 已验收。"""

from __future__ import annotations

import json
import os
import threading
import uuid
from contextlib import contextmanager
from dataclasses import replace
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest
from fpdf import FPDF

from knowledge.adapters.generator import StubGenerator
from knowledge.adapters.parser import build_parser_registry
from knowledge.domain.entities import Actor, BuildState
from knowledge.domain.errors import NotFound
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories.tables import IndexBuild
from knowledge.services import document, knowledge, qa, retrieval
from knowledge.worker.loop import process_next

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated DB required"),
]


@contextmanager
def contract_server(*, fail=False):
    calls = []

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def do_POST(self):
            body = self.rfile.read(int(self.headers["content-length"]))
            calls.append((body, self.headers.get("authorization")))
            self.send_response(422 if fail else 200)
            self.end_headers()
            self.wfile.write(
                json.dumps(
                    {
                        "profile": "deepdoc-v0.22.0-v1",
                        "blocks": [
                            {
                                "text": "会议室使用后必须关闭空调并清理现场。",
                                "page": 1,
                                "positions": [[1, 10, 200, 10, 50]],
                            }
                        ],
                        "warnings": ["deepdoc_ocr_requires_review", "deepdoc_table_linearized"],
                    }
                ).encode()
            )

    server = HTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_port}", calls
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


def setup_deepdoc(settings, deps, url):
    configured = settings.model_copy(
        update={
            "document_parser_profile": "deepdoc-v0.22.0-v1",
            "deepdoc_url": url,
            "deepdoc_token": "test-only",
        }
    )
    return configured, replace(
        deps, settings=configured, registry=build_parser_registry(configured)
    )


def upload(session, actor, settings, deps, kb, filename, data):
    return document.upload(
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


def test_deepdoc_http_publish_citation_warnings_authorization_and_delete(
    session,
    actor,
    settings,
    deps,
):
    with contract_server() as (url, calls):
        configured, configured_deps = setup_deepdoc(settings, deps, url)
        kb = knowledge.create(session, actor, "自制扫描协议夹具")
        result = upload(
            session,
            actor,
            configured,
            configured_deps,
            kb,
            "sample.png",
            b"\x89PNG\r\n\x1a\ncontract-fixture-not-real-image",
        )
        assert process_next(session, configured_deps)
        build = session.get(IndexBuild, result.build_id)
        assert build.state == BuildState.READY
        assert build.chunk_config["parser_profile"] == "deepdoc-v0.22.0-v1"
        assert calls[0][1] == "Bearer test-only"
        hits = retrieval.search(
            session, actor, configured, deps.embedder, query="会议室使用后", kb_ids=[kb.id]
        )
        assert "deepdoc_table_linearized" in hits[0].warnings
        assert hits[0].locator["positions"] == [[1, 10, 200, 10, 50]]
        answer = qa.answer(
            session,
            actor,
            configured,
            deps.embedder,
            StubGenerator(),
            question="会议室使用后",
            kb_ids=[kb.id],
        )
        assert "deepdoc_table_linearized" in answer.warnings
        assert answer.citations[0].locator["page"] == 1
        chunk_id = uuid.UUID(hits[0].chunk_id)
        assert (
            "deepdoc_table_linearized" in chunk_repo.get_owned(session, actor, chunk_id)["warnings"]
        )
        with pytest.raises(NotFound):
            chunk_repo.get_owned(session, Actor("other", actor.tenant_id), chunk_id)
        document.delete(session, actor, result.document_id, settings.max_attempts)
        with pytest.raises(NotFound):
            chunk_repo.get_owned(session, actor, chunk_id)
        assert not retrieval.search(
            session, actor, configured, deps.embedder, query="会议室使用后", kb_ids=[kb.id]
        )


def test_old_build_without_profile_does_not_switch_parser(session, actor, settings, deps):
    kb = knowledge.create(session, actor, "旧构建兼容")
    pdf = FPDF()
    pdf.add_page()
    pdf.set_font("Helvetica", size=12)
    pdf.cell(text="Self-authored legacy parser fixture.")
    result = upload(session, actor, settings, deps, kb, "room.pdf", bytes(pdf.output()))
    build = session.get(IndexBuild, result.build_id)
    build.chunk_config = {
        key: value for key, value in build.chunk_config.items() if key != "parser_profile"
    }
    session.commit()
    with contract_server(fail=True) as (url, calls):
        _, configured_deps = setup_deepdoc(settings, deps, url)
        assert process_next(session, configured_deps)
        assert session.get(IndexBuild, result.build_id).state == BuildState.READY
        assert calls == []


def test_deepdoc_failure_preserves_previous_published_build(session, actor, settings, deps):
    kb = knowledge.create(session, actor, "失败保留旧证据")
    result = upload(session, actor, settings, deps, kb, "room.txt", "会议室使用后关闭空调".encode())
    assert process_next(session, deps)
    with contract_server(fail=True) as (url, calls):
        configured, configured_deps = setup_deepdoc(settings, deps, url)
        newer = document.add_version(
            session,
            actor,
            configured,
            deps.storage,
            configured_deps.registry,
            document_id=result.document_id,
            filename="room.pdf",
            data=b"%PDF-contract-fixture",
            expected_generation=1,
            embedder_model=deps.embedder.model,
            embedder_dim=deps.embedder.dimension,
        )
        assert process_next(session, configured_deps)
        assert calls
        assert session.get(IndexBuild, newer.build_id).state == BuildState.FAILED
        assert session.get(IndexBuild, result.build_id).state == BuildState.READY
        hits = retrieval.search(
            session, actor, settings, deps.embedder, query="会议室使用后关闭空调", kb_ids=[kb.id]
        )
        assert {hit.build_id for hit in hits} == {str(result.build_id)}
