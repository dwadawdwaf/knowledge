"""固定自制资料的离线评测验收；验证工具，不把模型桩结果当领域效果。"""

import json

from scripts.evaluate_local import run_evaluation


def test_offline_evaluation_reports_metrics_not_quality_acceptance(runtime):
    report = run_evaluation(runtime)
    assert report["completed"] and report["mode"] == "fixture"
    assert len(report["cases"]) == 12
    assert report["search_qa_requests"] == 48
    assert report["quality_accepted"] is False
    assert report["human_review"] == "pending"
    assert report["cost"] is None
    assert all(
        set(case["retrieval"]) == {"keyword", "semantic", "hybrid"} for case in report["cases"]
    )
    by_id = {case["id"]: case for case in report["cases"]}
    assert by_id["F01"]["qa"]["fact_lexical_coverage"] == 1
    assert by_id["V01"]["qa"]["gold_support_coverage"] == 1
    # 哈希向量未召回含探针的长片段，必须报告未暴露，不可误判为抗注入通过。
    assert "instruction_probe_not_exposed" in by_id["I01"]["findings"]
    assert by_id["I01"]["retrieval"]["hybrid"]["instruction_marker_in_retrieved_evidence"] is False
    assert by_id["N02"]["findings"] == ["unanswerable_not_no_evidence"]
    assert report["finding_cases"] > 0
    serialized = json.dumps(report)
    assert "自制问题" not in serialized and "SYNTHETIC-BYPASS" not in serialized
    assert all("answer" not in case["qa"] and "question" not in case for case in report["cases"])
