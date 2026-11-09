"""评测工具测试：错误答案不能仅凭有效引用被认作有原文支持。"""

import json

import httpx
import pytest
from pydantic import ValidationError

from scripts.evaluate_local import evaluate_case, main
from scripts.evaluation import Case, answer_metrics, load_dataset, retrieval_metrics


def sample():
    return Case.model_validate(
        {
            "id": "T01",
            "category": "exact",
            "question": "自制问题",
            "facts": [{"aliases": ["350"], "document": "expense", "quote": "每晚 350 元"}],
        }
    )


def test_fixed_dataset_is_self_contained_and_bounded():
    data, fingerprint = load_dataset()
    assert len(data.cases) == 12 and len(fingerprint) == 64
    assert {case.category for case in data.cases} >= {
        "exact",
        "paraphrase",
        "multi_source",
        "identifier",
        "out_of_scope",
        "same_topic_unanswerable",
        "updated_version",
        "document_instruction",
    }
    assert len(data.cases) * 4 <= 60


@pytest.mark.parametrize(
    "facts,answerable",
    [
        ([], True),
        ([{"aliases": [""], "document": "room", "quote": "x"}], True),
        ([{"aliases": ["x"], "document": "room", "quote": "x"}], False),
    ],
)
def test_rejects_incomplete_gold_labels(facts, answerable):
    with pytest.raises(ValidationError):
        Case(id="T", category="test", question="test", facts=facts, answerable=answerable)


def test_wrong_fact_with_real_citation_is_not_correct():
    sources = {"expense": {"document_id": "d", "version_id": "v"}}
    metrics = answer_metrics(
        sample(),
        "报销上限是 999 元。[1]",
        "answered",
        [
            {"document_id": "d", "version_id": "v", "text": "每晚 350 元"},
        ],
        sources,
    )
    assert metrics["fact_lexical_coverage"] == 0
    assert metrics["gold_support_coverage"] == 1
    assert metrics["semantic_support_review"] == "pending_human_review"


@pytest.mark.parametrize("document,version", [("other", "v"), ("d", "old")])
def test_wrong_source_or_old_version_not_counted_as_support(document, version):
    sources = {"expense": {"document_id": "d", "version_id": "v"}}
    hit = {"document_id": document, "version_id": version, "excerpt": "每晚 350 元"}
    assert retrieval_metrics(sample(), [hit], sources)["gold_fact_recall_at_5"] == 0
    metrics = answer_metrics(
        sample(), "350 元", "answered", [{**hit, "text": hit["excerpt"]}], sources
    )
    assert metrics["gold_support_coverage"] == 0


def test_same_topic_false_answer_is_a_finding():
    case = Case(id="N", category="same_topic_unanswerable", question="人数?", answerable=False)
    metrics = answer_metrics(case, "资料内容。[1]", "answered", [], {})
    assert not metrics["status_matches_label"]
    assert metrics["fact_lexical_coverage"] is None


def test_source_instruction_marker_is_flagged():
    case = sample().model_copy(update={"forbidden": ["SYNTHETIC-BYPASS"]})
    metrics = answer_metrics(
        case,
        "SYNTHETIC-BYPASS",
        "answered",
        [],
        {"expense": {"document_id": "d", "version_id": "v"}},
    )
    assert metrics["forbidden_text_detected"]


@pytest.mark.parametrize("exposed", [False, True])
def test_instruction_probe_exposure_is_reported(exposed):
    marker = "SYNTHETIC-BYPASS"
    case = sample().model_copy(update={"category": "document_instruction", "forbidden": [marker]})
    hit = {
        "document_id": "d",
        "version_id": "v",
        "build_id": "b",
        "kb_id": "kb",
        "kb_name": "自制评测库",
        "chunk_id": "c",
        "title": "自制样本",
        "excerpt": f"每晚 350 元。{marker}" if exposed else "每晚 350 元。",
        "locator": {},
        "score": 0.1,
        "score_type": "rrf",
    }

    def respond(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/api/v1/search":
            mode = json.loads(request.content)["mode"]
            return httpx.Response(200, json={"data": {"mode": mode, "hits": [hit]}})
        return httpx.Response(200, json={"data": {"answer_status": "answered", "answer": "350 元"}})

    with httpx.Client(base_url="http://test", transport=httpx.MockTransport(respond)) as client:
        sources = {"expense": {"document_id": "d", "version_id": "v"}}
        result = evaluate_case(client, "kb", case, sources, {"search_qa_requests": 0})

    assert result["retrieval"]["hybrid"]["instruction_marker_in_retrieved_evidence"] is exposed
    assert ("instruction_probe_not_exposed" in result["findings"]) is (not exposed)
    assert marker not in json.dumps(result)


def test_provider_errors_are_not_refusal_and_raw_errors_are_redacted():
    with httpx.Client(
        base_url="http://test",
        transport=httpx.MockTransport(
            lambda _: httpx.Response(503, json={"error": {"message": "synthetic-secret"}})
        ),
    ) as client:
        report = {"search_qa_requests": 0}
        result = evaluate_case(client, "kb", sample(), {}, report)
    assert "qa_error_not_refusal" in result["findings"]
    assert "status_matches_label" not in result["qa"]
    assert "synthetic-secret" not in json.dumps(result)
    assert report["search_qa_requests"] == 4


def test_request_limit_prevents_actual_http_call():
    def unexpected(_):
        pytest.fail("达到上限后不得发起请求")

    with (
        httpx.Client(base_url="http://test", transport=httpx.MockTransport(unexpected)) as client,
        pytest.raises(ValueError, match="上限"),
    ):
        evaluate_case(client, "kb", sample(), {}, {"search_qa_requests": 60})


def test_real_mode_requires_explicit_confirmation(monkeypatch):
    monkeypatch.setattr("sys.argv", ["evaluate_local.py", "--real"])
    monkeypatch.setattr(
        "scripts.evaluate_local.load_real_settings", lambda: pytest.fail("不得读取配置")
    )
    assert main() == 1
