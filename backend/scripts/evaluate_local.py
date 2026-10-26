"""固定自制问题集的 HTTP 评测：默认离线；真实模式必须显式确认，不自动重跑。"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
import uuid
from contextlib import ExitStack
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean, median

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
from pydantic import ValidationError

from knowledge.api.v1.schemas.responses import AnswerOut, ChunkOut, SearchResultOut
from scripts.evaluation import FILES, Case, answer_metrics, load_dataset, retrieval_metrics
from scripts.http_scenario import expect, wait_job
from scripts.local_runtime import BACKEND, LocalRuntime
from scripts.model_fixture import ModelFixture
from scripts.real_model import load_real_settings

MAX_REQUESTS = 60


def run_evaluation(runtime: LocalRuntime) -> dict:
    dataset, fingerprint = load_dataset()
    report: dict = {
        "dataset_revision": dataset.revision,
        "dataset_sha256": fingerprint,
        "label_status": "developer_authored_pending_business_review",
        "started_at": datetime.now(UTC).isoformat(),
        "mode": "real" if runtime.model_settings else "fixture",
        "embedding_model": runtime.env["KS_EMBEDDING_MODEL"],
        "generator_model": runtime.env["KS_GENERATOR_MODEL"],
        "embedding_dim": 1024,
        "model_revision_verified": False,
        "retrieval_config": {
            "similarity_floor": float(runtime.env.get("KS_SIMILARITY_FLOOR", "0.06")),
            "semantic_max_distance": float(runtime.env.get("KS_SEMANTIC_MAX_DISTANCE", "0.85")),
            "evidence_min_score": float(runtime.env.get("KS_EVIDENCE_MIN_SCORE", "0")),
            "top_k": 5,
        },
        "search_qa_request_limit": MAX_REQUESTS,
        "search_qa_requests": 0,
        "token_usage": None,
        "cost": None,
        "usage_note": "既有 API 不返回用量，不能将未知费用记为零",
        "quality_accepted": False,
        "human_review": "pending",
        "cases": [],
        "completed": False,
    }
    with runtime.client() as client:
        kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "N3固定自制评测库"}), 201)
        sources: dict[str, dict] = {}

        def upload(key: str, *, updated: bool = False) -> None:
            path = FILES["room_updated" if updated else key]
            endpoint = (
                f"/api/v1/documents/{sources[key]['document_id']}/versions"
                if updated
                else f"/api/v1/knowledge-bases/{kb['id']}/documents"
            )
            result = expect(
                client.post(
                    endpoint, files={"file": (key + ".md", path.read_bytes(), "text/markdown")}
                ),
                202,
            )
            wait_job(client, result["job_id"], progress=runtime.say, timeout=180)
            sources[key] = result

        for key in ("room", "expense", "archive"):
            runtime.say(f"[N3 准备] 导入自制资料 {key}")
            upload(key)
        phase = "initial"
        for number, case in enumerate(dataset.cases, 1):
            if case.phase != phase:
                if phase != "initial" or case.phase != "updated":
                    raise ValueError("评测阶段顺序无效")
                runtime.say("[N3 版本] 上传版本二，等待发布后再评测，旧版不得参与召回")
                upload("room", updated=True)
                phase = "updated"
            runtime.say(f"[N3 {number}/{len(dataset.cases)}] {case.id} / {case.category}")
            result = evaluate_case(client, kb["id"], case, sources, report)
            report["cases"].append(result)
            runtime.say(
                f"[N3 {case.id}] 状态={result['qa'].get('status', 'error')}；发现={','.join(result['findings']) or '无自动发现'}"
            )
    report["completed"] = True
    report["finished_at"] = datetime.now(UTC).isoformat()
    report["finding_cases"] = sum(bool(case["findings"]) for case in report["cases"])
    positives = [case for case in report["cases"] if case["answerable"]]
    negatives = [case for case in report["cases"] if not case["answerable"]]
    report["retrieval_summary"] = {}
    for mode in ("keyword", "semantic", "hybrid"):
        rows = [case["retrieval"][mode] for case in positives]
        latencies = [
            case["retrieval"][mode]["elapsed_ms"]
            for case in report["cases"]
            if "elapsed_ms" in case["retrieval"][mode]
        ]
        report["retrieval_summary"][mode] = {
            "answerable_cases": len(rows),
            "mean_gold_fact_recall_at_5": mean(row.get("gold_fact_recall_at_5", 0) for row in rows),
            "mrr": mean(row.get("reciprocal_rank", 0) for row in rows),
            "top1_relevant_cases": sum(row.get("first_relevant_rank") == 1 for row in rows),
            "error_cases": sum("error" in case["retrieval"][mode] for case in report["cases"]),
            "latency_p50_ms": median(latencies) if latencies else None,
            "latency_sample_count": len(latencies),
        }
    report["qa_summary"] = {
        "answerable_cases": len(positives),
        "unanswerable_cases": len(negatives),
        "correct_refusals": sum(
            case["qa"].get("status_matches_label", False) for case in negatives
        ),
        "unanswerable_status_mismatches": sum(
            "unanswerable_not_no_evidence" in case["findings"] for case in negatives
        ),
        "error_cases": sum("error" in case["qa"] for case in report["cases"]),
        "all_facts_lexically_present": sum(
            case["qa"].get("fact_lexical_coverage") == 1 for case in positives
        ),
        "all_gold_facts_in_cited_sources": sum(
            case["qa"].get("gold_support_coverage") == 1 for case in positives
        ),
    }
    return report


def evaluate_case(
    client: httpx.Client, kb_id: str, case: Case, sources: dict[str, dict], report: dict
) -> dict:
    result: dict = {
        "id": case.id,
        "category": case.category,
        "answerable": case.answerable,
        "retrieval": {},
        "qa": {},
        "findings": [],
    }

    def post(path: str, body: dict) -> httpx.Response:
        if report["search_qa_requests"] >= MAX_REQUESTS:
            raise ValueError("已达到检索/问答请求上限，禁止自动追加调用")
        report["search_qa_requests"] += 1
        return client.post(path, json=body)

    for mode in ("keyword", "semantic", "hybrid"):
        started = time.monotonic()
        try:
            response = post(
                "/api/v1/search",
                {"query": case.question, "knowledge_base_ids": [kb_id], "mode": mode, "top_k": 5},
            )
            if response.status_code != 200:
                result["retrieval"][mode] = {
                    "http_status": response.status_code,
                    "error": "upstream_or_api_error",
                }
                result["findings"].append(f"{mode}_error")
                continue
            data = SearchResultOut.model_validate(response.json()["data"])
            metrics = retrieval_metrics(case, [hit.model_dump() for hit in data.hits], sources)
            metrics.update(
                elapsed_ms=round((time.monotonic() - started) * 1000, 1), degraded=data.degraded
            )
            result["retrieval"][mode] = metrics
            if case.category == "document_instruction" and mode == "hybrid":
                metrics["instruction_marker_in_retrieved_evidence"] = any(
                    marker in hit.excerpt for marker in case.forbidden for hit in data.hits
                )
                if not metrics["instruction_marker_in_retrieved_evidence"]:
                    result["findings"].append("instruction_probe_not_exposed")
            if data.degraded:
                result["findings"].append(f"{mode}_degraded")
            if mode == "hybrid" and case.answerable and metrics["gold_fact_recall_at_5"] < 1:
                result["findings"].append("retrieval_gold_missing")
        except (httpx.TransportError, ValidationError, KeyError, TypeError, json.JSONDecodeError):
            result["retrieval"][mode] = {"error": "transport_or_invalid_response"}
            result["findings"].append(f"{mode}_error")
    started = time.monotonic()
    try:
        response = post(
            "/api/v1/qa", {"question": case.question, "knowledge_base_ids": [kb_id], "top_k": 5}
        )
        if response.status_code != 200:
            result["qa"] = {"http_status": response.status_code, "error": "upstream_or_api_error"}
            result["findings"].append("qa_error_not_refusal")
            return result
        answer_data = AnswerOut.model_validate(response.json()["data"])
        cited = []
        for citation in answer_data.citations:
            source_response = client.get(f"/api/v1/chunks/{citation.chunk_id}")
            if source_response.status_code != 200:
                result["findings"].append("citation_unresolvable")
                continue
            chunk = ChunkOut.model_validate(source_response.json()["data"])
            if (
                any(
                    getattr(chunk, key) != getattr(citation, key)
                    for key in ("chunk_id", "document_id", "version_id", "build_id")
                )
                or citation.excerpt not in chunk.text
                or citation.locator != chunk.locator
            ):
                result["findings"].append("citation_mismatch")
                continue
            cited.append(chunk.model_dump(mode="json"))
        metrics = answer_metrics(
            case, answer_data.answer, answer_data.answer_status, cited, sources
        )
        metrics.update(
            status=answer_data.answer_status,
            citation_count=len(answer_data.citations),
            warning_count=len(answer_data.warnings),
            elapsed_ms=round((time.monotonic() - started) * 1000, 1),
        )
        result["qa"] = metrics
        if not metrics["status_matches_label"]:
            result["findings"].append(
                "unanswerable_not_no_evidence" if not case.answerable else "unexpected_refusal"
            )
        if case.answerable and metrics["fact_lexical_coverage"] < 1:
            result["findings"].append("fact_lexical_missing")
        if case.answerable and metrics["gold_support_coverage"] < 1:
            result["findings"].append("gold_citation_support_missing")
        if metrics["forbidden_text_detected"]:
            result["findings"].append("prohibited_text_needs_review")
        if answer_data.warnings:
            result["findings"].append("answer_warnings")
    except (httpx.TransportError, ValidationError, KeyError, TypeError, json.JSONDecodeError):
        result["qa"] = {"error": "transport_or_invalid_response"}
        result["findings"].append("qa_error_not_refusal")
    return result


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--real", action="store_true", help="使用现有真实模型配置")
    parser.add_argument(
        "--confirm-real", action="store_true", help="确认仅发送固定自制资料，并承担本次调用费用"
    )
    args = parser.parse_args()
    if args.real and not args.confirm_real:
        print("真实评测需要 --confirm-real；没有启动服务或调用模型。", flush=True)
        return 1
    try:
        with ExitStack() as stack:
            settings = load_real_settings() if args.real else None
            fixture = None if args.real else stack.enter_context(ModelFixture())
            runtime = stack.enter_context(
                LocalRuntime(
                    embedding_url=fixture.url if fixture else "",
                    generator_url=fixture.url if fixture else "",
                    model_settings=settings,
                )
            )
            report = run_evaluation(runtime)
        directory = BACKEND / ".data" / "evaluation" / uuid.uuid4().hex[:12]
        directory.mkdir(parents=True)
        path = directory / "report.json"
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        print(
            f"评测已执行：{len(report['cases'])} 用例，{report['search_qa_requests']}/60 次检索/问答请求。",
            flush=True,
        )
        print(
            f"发现问题的用例：{report['finding_cases']}；语义支持仍需人工复核，不代表业务效果验收。",
            flush=True,
        )
        print(f"脱敏指标报告：{path}", flush=True)
        return 2 if report["finding_cases"] else 0
    except (ValueError, RuntimeError, AssertionError, httpx.TransportError):
        print("评测未完整执行；请检查上方阶段和脱敏日志。不会自动重跑或输出内部异常。", flush=True)
        return 1
    except KeyboardInterrupt:
        print("评测已中止，正在清理本次临时服务。", flush=True)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
