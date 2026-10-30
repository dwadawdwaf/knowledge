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
from knowledge.services import document, knowledge, qa
from knowledge.worker.loop import process_next

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated DB required"),
]


@contextmanager
def parser_server():
    state = {"fail": False, "calls": 0}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_POST(self):
            self.rfile.read(int(self.headers["content-length"]))
            state["calls"] += 1
            self.send_response(503 if state["fail"] else 200)
            self.end_headers()
            if state["fail"]:
                return
            block = "合成设备保修期限为18个月。"
            if self.path == "/v1/parse":
                self.wfile.write(
                    json.dumps(
                        {
                            "profile": "deepdoc-v0.22.0-v2",
                            "warnings": [],
                            "blocks": [
                                {
                                    "text": block,
                                    "page": None,
                                    "positions": [],
                                    "warnings": ["deepdoc_native_location_unavailable"],
                                }
                            ],
                        }
                    ).encode()
                )
            else:
                stream = io.BytesIO()
                with zipfile.ZipFile(stream, "w") as archive:
                    archive.writestr(
                        "structured_content.json",
                        json.dumps(
                            {
                                "metadata": {"producer": {"name": "mineru", "version": "4.1.0"}},
                                "is_full_document": True,
                                "pages": [
                                    {"page_idx": 0, "blocks": [{"type": "text", "content": block}]}
                                ],
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


@pytest.mark.parametrize("profile", ["deepdoc-v0.22.0-v2", "mineru-api4.1-v1"])
def test_selected_parser_publish_dedup_rebuild_failure_and_authorization(
    session, actor, settings, deps, profile
):
    with parser_server() as (url, state):
        settings = settings.model_copy(
            update={"mineru_url": url, "deepdoc_url": url, "deepdoc_token": "synthetic"}
        )
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
