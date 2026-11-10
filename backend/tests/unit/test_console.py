from __future__ import annotations

import io
import json

import httpx
import pytest

from knowledge.config import Settings
from scripts.ask_console import ask_once, run_console
from scripts.real_model import (
    _RealSettings,
    load_real_settings,
    real_model_environment,
    validate_real_settings,
)


def configured_settings():
    return Settings.model_construct(
        embedding_base_url="http://127.0.0.1:1234/v1",
        embedding_model="bge-m3",
        generator_base_url="http://127.0.0.1:1235/v1",
        generator_model="real-test-model",
        embedding_api_key="synthetic-embedding-secret",
        generator_api_key="synthetic-generator-secret",
        database_url="postgresql://do-not-use",
        token_secret="do-not-use",
    )


@pytest.mark.parametrize(
    "overrides",
    [
        {"embedding_base_url": ""},
        {"generator_base_url": ""},
        {"embedding_model": "hash-1024"},
        {"generator_model": "stub"},
        {"embedding_dim": 3},
        {"generator_base_url": "file:///private"},
    ],
)
def test_real_mode_rejects_missing_or_stub_configuration(overrides):
    with pytest.raises(ValueError):
        validate_real_settings(configured_settings().model_copy(update=overrides))


def test_real_environment_does_not_forward_database_storage_or_identity():
    settings = configured_settings()
    validate_real_settings(settings)
    env = real_model_environment(settings)
    assert env["KS_EMBEDDING_API_KEY"] == "synthetic-embedding-secret"
    assert env["KS_GENERATOR_API_KEY"] == "synthetic-generator-secret"
    assert not {"KS_DATABASE_URL", "KS_STORAGE_ROOT", "KS_TOKEN_SECRET"} & env.keys()


def test_real_environment_preserves_parser_output_budget():
    settings = configured_settings().model_copy(update={"parse_output_bytes": 128 * 1024 * 1024})
    env = real_model_environment(settings)
    assert env["KS_PARSE_OUTPUT_BYTES"] == "134217728"
    assert int(env["KS_PARSE_OUTPUT_BYTES"]) == settings.parse_output_bytes


def test_real_environment_preserves_optimization_configuration():
    changes = {
        "chunk_max_chars": 700, "chunk_overlap_chars": 80,
        "retrieval_profile": "optimized-v2", "keyword_weight": 0.25, "keyword_local_floor": 0.35,
        "evidence_coverage": True, "structured_context": True,
        "semantic_weight": 1.0, "query_variants_max": 2, "recall_limit": 70, "rrf_k": 60,
        "evidence_candidate_limit": 30, "evidence_max_chars": 4000, "evidence_neighbor_limit": 1,
        "reranker_url": "http://127.0.0.1:8999/rerank", "reranker_model": "fixture",
        "reranker_timeout_seconds": 12,
    }
    env = real_model_environment(configured_settings().model_copy(update=changes))
    for name, value in changes.items():
        assert env["KS_" + name.upper()] == str(value)
    assert not {"KS_CHUNK_STRATEGY", "KS_PDF_CHUNK_STRATEGY"} & env.keys()
    assert not {"KS_DATABASE_URL", "KS_STORAGE_ROOT", "KS_TOKEN_SECRET"} & env.keys()


def test_console_displays_answer_and_server_citation():
    data = {
        "answer_status": "answered",
        "answer": "关闭照明。[1]",
        "warnings": [],
        "citations": [
            {
                "index": 1,
                "title": "自制资料.md",
                "excerpt": "使用后关闭照明。",
                "locator": {"type": "md", "char_start": 10, "char_end": 20},
            }
        ],
    }

    def handler(request):
        assert json.loads(request.content)["knowledge_base_ids"] == ["test-kb"]
        return httpx.Response(200, json={"data": data, "meta": {"request_id": "test-request"}})

    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local") as client:
        assert ask_once(client, "test-kb", "使用后怎么办？", output=output)
    text = output.getvalue()
    assert "关闭照明。[1]" in text and "自制资料.md" in text
    assert "使用后关闭照明。" in text and "10" in text


@pytest.mark.parametrize(
    "status,code",
    [(503, "MODEL_UNAVAILABLE"), (504, "MODEL_TIMEOUT"), (502, "CITATION_VALIDATION_FAILED")],
)
def test_console_errors_are_not_rendered_as_no_evidence(status, code):
    def handler(request):
        return httpx.Response(
            status,
            json={
                "error": {"code": code, "message": "sensitive details"},
                "meta": {"request_id": "test-request"},
            },
        )

    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local") as client:
        assert not ask_once(client, "test-kb", "synthetic", output=output)
    assert code in output.getvalue()
    assert "sensitive details" not in output.getvalue()
    assert "no_evidence" not in output.getvalue()


def test_empty_console_input_does_not_call_api():
    answers = iter(["", "/quit"])

    def handler(request):
        pytest.fail("Empty input must not call the API")

    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local") as client:
        run_console(client, "test-kb", read=lambda _: next(answers), output=io.StringIO())


def test_no_evidence_has_no_fake_citation():
    def handler(request):
        return httpx.Response(
            200,
            json={
                "data": {
                    "answer_status": "no_evidence",
                    "answer": "资料中未找到相关依据。",
                    "citations": [],
                }
            },
        )

    output = io.StringIO()
    with httpx.Client(transport=httpx.MockTransport(handler), base_url="http://local") as client:
        assert ask_once(client, "test-kb", "synthetic", output=output)
    assert "no_evidence" in output.getvalue() and "引用：无。" in output.getvalue()


def test_real_env_file_ignores_blank_optional_fields(monkeypatch, tmp_path):
    import os

    for key in list(os.environ):
        if key.startswith("KS_"):
            monkeypatch.delenv(key)
    config = tmp_path / ".env"
    config.write_text(
        "KS_EMBEDDING_BASE_URL=http://127.0.0.1:1/v1\n"
        "KS_EMBEDDING_MODEL=bge-m3\nKS_EMBEDDING_DIMENSIONS=\n"
        "KS_GENERATOR_BASE_URL=http://127.0.0.1:2/v1\n"
        "KS_GENERATOR_MODEL=real-test-model\n",
        encoding="utf-8",
    )
    monkeypatch.setitem(_RealSettings.model_config, "env_file", config)
    settings = load_real_settings()
    assert settings.embedding_dimensions is None
    assert settings.generator_model == "real-test-model"
