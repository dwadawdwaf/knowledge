"""评测标注、指标和清空边界；不调用真实模型、不访问业务资料。"""

import io
from unittest.mock import Mock

import pytest
from docx import Document
from openpyxl import load_workbook

from scripts import retrieval_baseline as baseline
from scripts.evaluate_public import contained_file


def test_dataset_is_bounded_and_labels_exist():
    dataset = baseline.Dataset.model_validate_json(baseline.DATASET.read_bytes())
    assert len(dataset.cases) == 24
    assert len({case.category for case in dataset.cases}) == 8
    assert len(dataset.cases) * (len(baseline.VARIANTS) + 1) == baseline.MAX_REQUESTS
    assert sum(not case.facts for case in dataset.cases) == 3
    assert len({source.name.rsplit(".", 1)[1] for source in dataset.documents}) == 5


def test_office_sources_preserve_synthetic_labels():
    dataset = baseline.Dataset.model_validate_json(baseline.DATASET.read_bytes())
    source = next(source for source in dataset.documents if source.key == "expense")
    document = Document(io.BytesIO(baseline.source_bytes(source)))
    assert "380元" in "\n".join(paragraph.text for paragraph in document.paragraphs)
    source = next(source for source in dataset.documents if source.key == "inventory")
    workbook = load_workbook(io.BytesIO(baseline.source_bytes(source)))
    assert list(workbook.active.values)[1] == ("EQ-NX-803", "便携投影仪", "北楼302", "7", "3")


def test_recall_requires_current_source_and_all_facts():
    case = baseline.Case(
        id="X",
        category="跨来源",
        question="自制问题",
        facts=[
            baseline.Fact(document="a", quote="事实甲", aliases=["甲"]),
            baseline.Fact(document="b", quote="事实乙", aliases=["乙"]),
        ],
    )
    sources = {
        "a": {"document_id": "a", "build_id": "new"},
        "b": {"document_id": "b", "build_id": "new"},
    }
    hits = [
        {"document_id": "a", "build_id": "new", "excerpt": "事实甲"},
        {"document_id": "b", "build_id": "old", "excerpt": "事实乙"},
    ]
    metrics = baseline.retrieval_metrics(case, hits, sources)
    assert metrics["fact_recall"] == 0.5
    assert metrics["mrr"] == 1
    assert not metrics["all_facts_found"]


def test_negative_cases_do_not_receive_fake_perfect_recall():
    case = baseline.Case(id="N", category="无答案", question="自制问题", facts=[])
    metrics = baseline.retrieval_metrics(case, [], {})
    assert metrics["fact_recall"] is None
    assert metrics["mrr"] is None
    assert metrics["all_facts_found"] is None


def test_pdf_matching_is_explicit_and_requires_physical_page():
    fact = baseline.Fact(document="a", quote="ＡI 42", aliases=["42"], pdf_pages=[7])
    assert not baseline.supports_fact(fact, "ai\n42", {"page": 7})
    fact.normalized = True
    assert baseline.supports_fact(fact, "ai\n42", {"page": 7})
    assert not baseline.supports_fact(fact, "ai\n42", {"page": 8})
    assert not baseline.supports_fact(fact, "ai\n42", {})
    assert not baseline.supports_fact(fact, "ai\n43", {"page": 7})


def test_oversized_experiment_is_rejected_before_client_use():
    dataset = baseline.Dataset.model_validate_json(baseline.DATASET.read_bytes())
    runtime = Mock()
    with pytest.raises(ValueError, match="预算"):
        baseline.run_experiment(runtime, dataset, spec=baseline.ExperimentSpec(request_limit=119))
    runtime.client.assert_not_called()


def test_public_files_cannot_escape_selected_corpus(tmp_path):
    root = tmp_path / "corpus"
    root.mkdir()
    sample = root / "sample.txt"
    sample.write_text("synthetic", encoding="utf-8")
    outside = tmp_path / "outside.txt"
    outside.write_text("synthetic", encoding="utf-8")
    assert contained_file(root, "sample.txt") == sample
    for name in ("../outside.txt", str(outside), "missing.txt"):
        with pytest.raises(ValueError, match="越界或缺失"):
            contained_file(root, name)


def test_dataset_accepts_48_but_rejects_49_synthetic_cases():
    from pydantic import ValidationError

    payload = {
        "revision": "synthetic",
        "documents": [],
        "cases": [
            {"id": str(index), "category": "自制", "question": "自制问题", "facts": []}
            for index in range(48)
        ],
    }
    assert len(baseline.Dataset.model_validate(payload).cases) == 48
    payload["cases"].append({"id": "49", "category": "自制", "question": "自制", "facts": []})
    with pytest.raises(ValidationError):
        baseline.Dataset.model_validate(payload)


def test_summary_separates_errors_from_valid_metric_denominator():
    cases = []
    for error in (False, True):
        cases.append(
            {
                "answerable": True,
                "retrieval": {
                    f"{mode}_{size}": {"error": "MODEL_UNAVAILABLE"}
                    if error
                    else {"fact_recall": 0.5, "mrr": 1, "all_facts_found": False}
                    for mode, size in baseline.VARIANTS
                },
            }
        )
    summary = baseline.summarize(cases)["hybrid_5"]
    assert summary["positive_cases"] == 2
    assert summary["valid_cases"] == 1
    assert summary["errors"] == 1
    assert summary["mean_fact_recall"] == 0.5


@pytest.mark.parametrize(
    "url", ["postgresql://postgres@remote/knowledge", "postgresql://postgres@127.0.0.1/other"]
)
def test_reset_rejects_other_databases(monkeypatch, tmp_path, url):
    recorded = tmp_path / "db_url"
    recorded.write_text("postgresql+psycopg://postgres@127.0.0.1:55433/knowledge")
    monkeypatch.setattr(baseline.local_db, "DB_URL_FILE", recorded)
    stop = Mock()
    monkeypatch.setattr(baseline, "stop_project_services", stop)
    with pytest.raises(ValueError, match="拒绝重置"):
        baseline.backup_and_reset(url, tmp_path / "objects")
    stop.assert_not_called()
