"""评测工具的离线端到端验收，不把桩指标当真实模型效果。"""

import json

import pytest

from scripts import retrieval_baseline as baseline
from scripts.http_scenario import expect, wait_job


def test_persistent_report_history_scope_and_delete(runtime, monkeypatch, tmp_path):
    monkeypatch.setattr(baseline, "open_workbench", lambda _: None)
    dataset = baseline.Dataset.model_validate_json(baseline.DATASET.read_bytes())
    report = baseline.run_experiment(runtime, dataset, report_directory=tmp_path)
    assert report["status"] == "completed"
    assert report["requests"] == 120
    assert len(report["cases"]) == 24
    assert len(report["sources"]) == 8
    assert report["label_status"] == "developer_authored_pending_review"
    assert all("answer" not in row["qa"] for row in report["cases"])
    assert json.loads((tmp_path / "report.json").read_bytes()) == report
    with runtime.client() as client, runtime.client("eval-other") as other:
        libraries = expect(client.get("/api/v1/knowledge-bases"), 200)["items"]
        reports = next(kb for kb in libraries if kb["name"] == baseline.REPORT_LIBRARY)
        documents = expect(client.get(f"/api/v1/knowledge-bases/{reports['id']}/documents"), 200)[
            "items"
        ]
        document = next(doc for doc in documents if doc["title"] == baseline.REPORT_FILE)
        assert len(documents) == 1
        source = client.get(f"/api/v1/documents/{document['id']}/file")
        assert source.status_code == 200
        assert json.loads(source.content) == report
        assert other.get(f"/api/v1/documents/{document['id']}/file").status_code == 404
        conversation_id = report["cases"][0]["conversation_id"]
        history = expect(client.get(f"/api/v1/conversations/{conversation_id}"), 200)
        assert len(history["messages"]) == 6
        assert other.get(f"/api/v1/conversations/{conversation_id}").status_code == 404
        second = baseline.Dataset(
            revision="synthetic-second",
            documents=dataset.documents[:1],
            cases=[dataset.cases[0]],
        )
        existing = expect(client.post("/api/v1/knowledge-bases", json={"name": "自制第二轮"}), 201)
        uploaded = expect(
            client.post(
                f"/api/v1/knowledge-bases/{existing['id']}/documents",
                files={
                    "file": (second.documents[0].name, baseline.source_bytes(second.documents[0]))
                },
                data={"parser_profile": "legacy-v1"},
            ),
            202,
        )
        wait_job(client, uploaded["job_id"], progress=runtime.say)
        second_spec = baseline.ExperimentSpec(
            kb_name="自制第二轮",
            report_file="retrieval-evaluation-v2-public.txt",
            request_limit=5,
            add_noise=False,
            compact_report=True,
        )
        second_report = baseline.run_experiment(
            runtime,
            second,
            report_directory=tmp_path / "second",
            spec=second_spec,
            resume_ingestion=True,
        )
        assert second_report["requests"] == 5
        assert second_report["knowledge_base_id"] == existing["id"]
        assert second_report["sources"][0]["build_id"] == uploaded["build_id"]
        with pytest.raises(ValueError, match="已有本轮报告"):
            baseline.run_experiment(runtime, second, spec=second_spec, resume_ingestion=True)
        libraries_after = expect(client.get("/api/v1/knowledge-bases"), 200)["items"]
        assert sum(kb["name"] == baseline.REPORT_LIBRARY for kb in libraries_after) == 1
        documents_after = expect(
            client.get(f"/api/v1/knowledge-bases/{reports['id']}/documents"), 200
        )["items"]
        assert len(documents_after) == 2
        assert client.get(f"/api/v1/documents/{document['id']}/file").content == source.content
        expect(client.delete(f"/api/v1/knowledge-bases/{reports['id']}"), 202)
        assert client.get(f"/api/v1/documents/{document['id']}/file").status_code == 404
