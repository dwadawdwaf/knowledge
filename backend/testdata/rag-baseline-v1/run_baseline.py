#!/usr/bin/env python3
"""在隔离数据库中导入 rag-baseline-v1，并测量分块与检索基线。

默认使用真实向量模型（读取 backend/.env），生成器使用本地桩，不调用大模型；
PNG 与扫描/双栏 PDF 需要本机 MinerU，默认跳过并用 `--include-mineru` 显式开启。
运行：
    backend/.venv/Scripts/python.exe testdata/rag-baseline-v1/run_baseline.py
"""

from __future__ import annotations

import argparse
import concurrent.futures
import hashlib
import json
import math
import mimetypes
import os
import pathlib
import platform
import statistics
import sys
import threading
import time
import unicodedata
from collections import Counter, defaultdict
from typing import Any

BACKEND = pathlib.Path(__file__).resolve().parents[2]
sys.path.insert(0, str(BACKEND))
os.environ.setdefault("NLTK_DATA", str(BACKEND / ".data" / "ragflow-resources"))
os.environ.setdefault("TIKTOKEN_CACHE_DIR", str(BACKEND / ".data" / "ragflow-deps"))

from scripts.http_scenario import expect, wait_job
from scripts.local_runtime import LocalRuntime
from scripts.model_fixture import ModelFixture
from scripts.real_model import load_real_settings

ROOT = pathlib.Path(__file__).resolve().parent
DEFAULT_OUT = BACKEND / ".data" / "rag-baseline-20261111"


def normalized(value: str | None) -> str:
    if not value:
        return ""
    return "".join(unicodedata.normalize("NFKC", value).lower().split())


def percentile(values: list[float], ratio: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    return ordered[max(0, math.ceil(len(ordered) * ratio) - 1)]


def save(path: pathlib.Path, value: Any) -> None:
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, path)


def load_manifest() -> dict[str, Any]:
    manifest = json.loads((ROOT / "manifest.json").read_text(encoding="utf-8"))
    for document in manifest["documents"]:
        path = ROOT / document["file"]
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        if digest != document["sha256"]:
            raise SystemExit(f"语料哈希不一致，请重新生成冻结集：{document['file']}")
    return manifest


def import_documents(client, kb_id: str, manifest: dict[str, Any], include_mineru: bool, progress, pdf_parser: str = "legacy") -> tuple[dict[str, Any], dict[str, dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    chunks: dict[str, dict[str, Any]] = {}
    skipped: list[dict[str, Any]] = []
    for index, document in enumerate(manifest["documents"], 1):
        if document["requires_mineru"] and not include_mineru:
            skipped.append({"id": document["id"], "file": document["file"], "reason": "requires MinerU (images/scanned or multi-column PDF)"})
            continue
        path = ROOT / document["file"]
        started = time.perf_counter()
        try:
            if document["requires_mineru"]:
                # PNG 默认走 MinerU；扫描/双栏 PDF 显式指定，避免 auto 路由歧义。
                form = {"parser_profile": "mineru-api4.1-v1"} if document["format"] == "pdf" else None
            elif document["format"] == "pdf" and pdf_parser == "legacy":
                form = {"parser_profile": "legacy-v1"}
            else:
                form = None
            ack = expect(
                client.post(
                    f"/api/v1/knowledge-bases/{kb_id}/documents",
                    files={"file": (path.name, path.read_bytes(), mimetypes.guess_type(path.name)[0] or "application/octet-stream")},
                    data=form,
                ),
                202,
            )
            wait_job(client, ack["job_id"], progress=lambda _: None, timeout=900)
            view = expect(client.get(f"/api/v1/documents/{ack['document_id']}/chunks?limit=500"), 200)
            for chunk in view["items"]:
                chunks[chunk["chunk_id"]] = {
                    **chunk,
                    "file": document["file"],
                    "doc_id": document["id"],
                    "format": document["format"],
                }
            record = {
                "id": document["id"],
                "file": document["file"],
                "format": document["format"],
                "lang": document["lang"],
                "theme": document["theme"],
                "status": "succeeded",
                "chunk_count": view["total"],
                "truncated": view["truncated"],
                "seconds": round(time.perf_counter() - started, 3),
                "parser_profile": ack.get("parser_profile"),
                "warnings": sorted({warning for chunk in view["items"] for warning in chunk.get("warnings", [])}),
            }
        except Exception as exc:  # noqa: BLE001 - 逐份保留失败，不把失败当通过
            record = {
                "id": document["id"],
                "file": document["file"],
                "format": document["format"],
                "lang": document["lang"],
                "theme": document["theme"],
                "status": "failed",
                "seconds": round(time.perf_counter() - started, 3),
                "error_type": type(exc).__name__,
                "error": str(exc)[:500],
            }
        records.append(record)
        progress(f"[{index}/{len(manifest['documents'])}] {document['id']} {record['status']} chunks={record.get('chunk_count', 0)}")
    return {"documents": records, "skipped": skipped}, chunks, records


def search_once(client, kb_id: str, query: str, mode: str, top_k: int, retries: int = 4) -> tuple[list[dict[str, Any]] | None, str | None, float, list[str]]:
    started = time.perf_counter()
    for attempt in range(retries):
        try:
            response = client.post(
                "/api/v1/search",
                json={"query": query, "knowledge_base_ids": [kb_id], "mode": mode, "top_k": top_k},
            )
        except Exception as exc:  # noqa: BLE001 - 网络错误按重试处理
            if attempt == retries - 1:
                return None, f"transport:{type(exc).__name__}", (time.perf_counter() - started) * 1000, []
            time.sleep(1.2 * (attempt + 1))
            continue
        if response.status_code == 200:
            payload = response.json()
            if isinstance(payload.get("data"), dict):
                payload = payload["data"]
            return payload["hits"], None, (time.perf_counter() - started) * 1000, list(payload.get("warnings") or [])
        if response.status_code in (429, 500, 502, 503, 504) and attempt < retries - 1:
            time.sleep(1.5 * (attempt + 1))
            continue
        return None, f"http:{response.status_code}", (time.perf_counter() - started) * 1000, []
    return None, "retries_exhausted", (time.perf_counter() - started) * 1000, []


def evaluate_hits(query: dict[str, Any], hits: list[dict[str, Any]], chunks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    needles = [normalized(needle) for needle in query["gold"]["needles"]]
    same_chunk = query["gold"]["same_chunk"]
    texts: list[tuple[int, str]] = []
    for rank, hit in enumerate(hits, 1):
        chunk = chunks.get(hit["chunk_id"], {})
        texts.append((rank, normalized(chunk.get("text") or hit.get("excerpt"))))
    per_rank: list[bool] = []
    any_rank: list[bool] = []
    for _, text in texts:
        if same_chunk:
            per_rank.append(all(needle in text for needle in needles))
        else:
            # 跨节问题按累计覆盖计算，位置取覆盖完整的第一个排名。
            per_rank.append(False)
        any_rank.append(any(needle in text for needle in needles))
    first_all = next((rank for rank, ok in enumerate(per_rank, 1) if ok), None)
    if not same_chunk and needles:
        seen: set[str] = set()
        cumulative: list[bool] = []
        for _, text in texts:
            for needle in needles:
                if needle in text:
                    seen.add(needle)
            cumulative.append(len(seen) == len(needles))
        first_all = next((rank for rank, ok in enumerate(cumulative, 1) if ok), None)
        per_rank = cumulative
    first_any = next((rank for rank, ok in enumerate(any_rank, 1) if ok), None)
    return {
        "hit@1": bool(per_rank[0]) if per_rank else False,
        "hit@5": any(per_rank[:5]),
        "hit@20": any(per_rank[:20]),
        "any@5": any(any_rank[:5]),
        "first_all_rank": first_all,
        "first_any_rank": first_any,
        "mrr": 1 / first_all if first_all else 0.0,
        "ranked": [
            {
                "rank": rank,
                "file": chunks.get(hit["chunk_id"], {}).get("file"),
                "chunk_id": hit["chunk_id"],
                "score": hit["score"],
                "locator": hit.get("locator"),
                "excerpt": (hit.get("excerpt") or "")[:300],
            }
            for rank, hit in enumerate(hits, 1)
        ],
    }


def chunk_statistics(manifest: dict[str, Any], records: list[dict[str, Any]], chunks: dict[str, dict[str, Any]]) -> dict[str, Any]:
    by_doc: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for chunk in chunks.values():
        by_doc[chunk["doc_id"]].append(chunk)
    succeeded = [record for record in records if record["status"] == "succeeded"]
    lengths = [len(chunk["text"]) for chunk in chunks.values()]
    empty = sum(1 for length in lengths if length == 0)
    short = sum(1 for length in lengths if 0 < length < 50)
    long = sum(1 for length in lengths if length > 4000)
    duplicates = sum(count - 1 for count in Counter(normalized(chunk["text"]) for chunk in chunks.values()).values() if count > 1)
    coverage: list[float] = []
    coverage_failures: list[dict[str, Any]] = []
    for document in manifest["documents"]:
        terms = document.get("check_terms") or []
        doc_chunks = by_doc.get(document["id"], [])
        if not terms or not doc_chunks:
            continue
        text = " ".join(normalized(chunk.get("text")) for chunk in doc_chunks)
        found = sum(1 for term in terms if normalized(term) in text)
        ratio = found / len(terms)
        coverage.append(ratio)
        if ratio < 1:
            coverage_failures.append({"id": document["id"], "ratio": round(ratio, 3), "missing": [term for term in terms if normalized(term) not in text][:8]})
    table_checks: list[dict[str, Any]] = []
    for document in manifest["documents"]:
        gold = document.get("gold_row")
        if not gold or document["format"] not in ("csv", "xlsx"):
            continue
        doc_chunks = by_doc.get(document["id"], [])
        matching = [chunk for chunk in doc_chunks if normalized(gold["code"]) in normalized(chunk["text"])]
        same = [chunk for chunk in matching if all(normalized(value) in normalized(chunk["text"]) for value in (gold["owner"], gold["place"]))]
        locators = [chunk.get("locator") or {} for chunk in matching]
        table_checks.append({
            "id": document["id"],
            "format": document["format"],
            "gold_chunks": len(matching),
            "owner_and_place_same_chunk": bool(same),
            "locator_has_sheet": any(locator.get("sheet") for locator in locators),
            "locator_rows": [{"sheet": locator.get("sheet"), "row_start": locator.get("row_start"), "row_end": locator.get("row_end")} for locator in locators],
        })
    return {
        "documents": len(succeeded),
        "chunks": len(chunks),
        "length": {
            "min": min(lengths) if lengths else 0,
            "median": statistics.median(lengths) if lengths else 0,
            "mean": round(statistics.fmean(lengths), 1) if lengths else 0,
            "p95": percentile(lengths, 0.95),
            "max": max(lengths) if lengths else 0,
            "empty": empty,
            "under_50": short,
            "over_4000": long,
        },
        "duplicate_chunks": duplicates,
        "per_doc": [
            {
                "id": record["id"],
                "format": record["format"],
                "lang": record["lang"],
                "chunks": record.get("chunk_count", 0),
                "chars": sum(len(chunk["text"]) for chunk in by_doc.get(record["id"], [])),
                "median_chars": statistics.median([len(chunk["text"]) for chunk in by_doc.get(record["id"], [])]) if by_doc.get(record["id"]) else 0,
                "under_50": sum(1 for chunk in by_doc.get(record["id"], []) if 0 < len(chunk["text"]) < 50),
                "warnings": record.get("warnings", []),
            }
            for record in succeeded
        ],
        "section_coverage_mean": round(statistics.fmean(coverage), 4) if coverage else None,
        "section_coverage_failures": coverage_failures,
        "table_rows": table_checks,
    }


def aggregate(rows: list[dict[str, Any]], modes: list[str]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for mode in modes:
        pairs = [(row, row["modes"].get(mode)) for row in rows if row["modes"].get(mode) is not None]
        valid = [(row, value) for row, value in pairs if value.get("hits") is not None]
        positives = [value for row, value in valid if row["answerable"]]
        negatives = [value for row, value in valid if not row["answerable"]]
        latencies = [value["ms"] for _, value in pairs]
        result[mode] = {
            "queries": len(pairs),
            "positive": len(positives),
            "negative": len(negatives),
            "hit@1": round(sum(value["hit@1"] for value in positives) / len(positives), 4) if positives else None,
            "hit@5": round(sum(value["hit@5"] for value in positives) / len(positives), 4) if positives else None,
            "hit@20": round(sum(value["hit@20"] for value in positives) / len(positives), 4) if positives else None,
            "any@5": round(sum(value["any@5"] for value in positives) / len(positives), 4) if positives else None,
            "mrr": round(statistics.fmean([value["mrr"] for value in positives]), 4) if positives else None,
            "no_answer_with_candidates": sum(1 for value in negatives if value["count"] > 0),
            "no_answer_total": len(negatives),
            "no_answer_max_score_mean": round(statistics.fmean([value["max_score"] for value in negatives]), 4) if negatives else None,
            "latency_ms": {
                "p50": round(percentile(latencies, 0.5), 1),
                "p95": round(percentile(latencies, 0.95), 1),
                "max": round(max(latencies), 1) if latencies else 0,
            },
            "errors": sum(1 for _, value in pairs if value.get("error")),
        }
    return result


def write_report(path: pathlib.Path, summary: dict[str, Any], results: dict[str, Any]) -> None:
    counts = summary["corpus"]
    lines = [
        "# RAG 分块与检索基线报告",
        "",
        f"- 运行时间：{summary['started_at']}",
        f"- 语料：{counts['documents']} 份（中文 {counts['zh']} / 英文 {counts['en']}），查询 {counts['queries']} 道（可答 {counts['positive']} / 无答案 {counts['negative']}）",
        f"- 环境：{summary['environment']}",
        f"- 检索组件：{summary.get('search_engine', 'postgresql')}" + (
            f"（{summary['opensearch']['url']}，索引 `{summary['opensearch']['index']}`）"
            if summary.get("opensearch") else "（PostgreSQL 词项召回）"
        ),
        f"- 分块：{summary['configuration']['chunk_profile']}，目标 {summary['configuration']['chunk_tokens']} tokens，零重叠；生成器：本地桩（不评估生成质量）",
        "",
        "## 1. 解析与分块",
        "",
        f"- 成功入库 {summary['chunking']['documents']} 份，失败 {summary['documents_failed']} 份，跳过 {summary['documents_skipped']} 份（需要 MinerU）。",
        f"- MinerU OCR：{summary.get('ocr_documents', 0)} 份文档、{summary.get('ocr_chunks', 0)} 个分块。",
        f"- 共 {summary['chunking']['chunks']} 个分块；长度中位数 {summary['chunking']['length']['median']} 字符，P95 {summary['chunking']['length']['p95']}，最大 {summary['chunking']['length']['max']}。",
        f"- 空分块 {summary['chunking']['length']['empty']}，少于 50 字符 {summary['chunking']['length']['under_50']}，超过 4000 字符 {summary['chunking']['length']['over_4000']}，重复分块 {summary['chunking']['duplicate_chunks']}。",
        f"- 章节/表头保留率均值：{summary['chunking']['section_coverage_mean']}（仅统计成功入库文档）；未完全保留的文档 {len(summary['chunking']['section_coverage_failures'])} 份。",
        "",
        "分格式分块：",
        "",
        "| 格式 | 文档 | 分块 | 每文档分块中位数 | 字符中位数 | 少于50字符 |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    format_stats: dict[str, dict[str, Any]] = defaultdict(lambda: {"docs": 0, "chunks": 0, "per_doc": [], "chars": [], "under_50": 0})
    for document in summary["chunking"]["per_doc"]:
        stats = format_stats[document["format"]]
        stats["docs"] += 1
        stats["chunks"] += document["chunks"]
        stats["per_doc"].append(document["chunks"])
        stats["chars"].append(document["median_chars"])
        stats["under_50"] += document.get("under_50", 0)
    for fmt, stats in sorted(format_stats.items()):
        lines.append(f"| {fmt} | {stats['docs']} | {stats['chunks']} | {statistics.median(stats['per_doc']) if stats['per_doc'] else 0} | {statistics.median(stats['chars']) if stats['chars'] else 0} | {stats['under_50']} |")
    lines += [
        "",
        "## 2. 检索指标",
        "",
        "| 模式 | Hit@1 | Hit@5 | Hit@20 | 任意证据@5 | MRR | 无答案有候选 | 无答案最高分均值 | P50 ms | P95 ms | 错误 |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for mode, value in summary["modes"].items():
        lines.append(
            f"| {mode} | {value['hit@1']} | {value['hit@5']} | {value['hit@20']} | {value['any@5']} | {value['mrr']} | "
            f"{value['no_answer_with_candidates']}/{value['no_answer_total']} | {value['no_answer_max_score_mean']} | "
            f"{value['latency_ms']['p50']} | {value['latency_ms']['p95']} | {value['errors']} |"
        )
    lines += [
        "",
        "Hit@K 表示同一分块（跨节题则 Top-K 并集）完整包含全部金标准证据；任意证据@5 表示只命中部分证据。",
        f"延迟在 {summary['workers']} 个并发客户端下测得，仅用于相对比较，不代表串行或生产环境的 P95。",
        "",
        "## 3. 分维度",
        "",
    ]
    for dimension in ("format", "lang", "type"):
        lines += [f"### 按 {dimension}", "", "| 分组 | 题数 | " + " | ".join(f"{mode} Hit@5" for mode in summary["modes"]) + " |", "| --- | --- | " + " | ".join("---" for _ in summary["modes"]) + " |"]
        for group, value in summary["breakdown"][dimension].items():
            lines.append(f"| {group} | {value['count']} | " + " | ".join(str(value['modes'][mode]['hit@5']) for mode in summary["modes"]) + " |")
        lines.append("")
    lines += [
        "## 4. 主要失败与限制",
        "",
        (
            f"- 跳过文档：{summary['documents_skipped']} 份，主要为 PNG 与扫描/双栏 PDF；默认运行不启动 MinerU，未计入分块与检索指标。"
            if summary["documents_skipped"]
            else "- 跳过文档：0 份；MinerU 已覆盖 12 张 PNG 与 2 份双栏/扫描 PDF。"
        ),
        "- PDF 解析：普通 PDF 使用本地 `legacy-v1`（pypdf）；双栏/扫描 PDF 显式使用 MinerU，二者分块粒度不同。",
        f"- 失败文档：{summary['documents_failed']} 份；详见 results.json 的 documents 字段。",
        f"- 查询错误：{summary['query_errors']} 个（模式级）；按 429/5xx 自动重试后仍失败才计入。",
        "",
    ]
    if results.get("skipped"):
        lines.append("跳过的文档：" + "、".join(item["id"] for item in results["skipped"][:20]) + ("……" if len(results["skipped"]) > 20 else ""))
        lines.append("")
    lines += [
        "未命中题目（按 hybrid，最多 20 条）：",
        "",
    ]
    report_mode = "hybrid" if "hybrid" in summary["modes"] else next(iter(summary["modes"]))
    misses = [row for row in results["queries"] if row["answerable"] and row.get("modes", {}).get(report_mode, {}).get("hits") is not None and not row["modes"][report_mode]["hit@5"]]
    for row in misses[:20]:
        lines.append(f"- `{row['id']}` {row['query']}（金标准：{row['gold']['value']}）")
    if not misses:
        lines.append("- 无。")
    lines += [
        "",
        "## 5. 复跑",
        "",
        "```powershell",
        ".\\backend\\.venv\\Scripts\\python.exe testdata/rag-baseline-v1/run_baseline.py",
        "# 本机 MinerU 就绪后纳入图片与扫描件：",
        ".\\backend\\.venv\\Scripts\\python.exe testdata/rag-baseline-v1/run_baseline.py --include-mineru",
        "```",
        "",
        "完整逐题排名、分块与逐文档结果见同目录 `results.json`、`summary.json`、`chunks.json`。",
    ]
    path.write_text("\n".join(lines), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-mineru", action="store_true", help="包含 PNG、扫描 PDF 与双栏 PDF（需要本机 MinerU）")
    parser.add_argument("--limit-docs", type=int, default=None)
    parser.add_argument("--limit-queries", type=int, default=None)
    parser.add_argument("--doc-start", type=int, default=0)
    parser.add_argument("--doc-count", type=int, default=None)
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--queries-only", action="store_true")
    parser.add_argument("--pg-data", default=None, help="持久化 embedded PostgreSQL 数据目录；跨多次短运行复用")
    parser.add_argument("--storage-root", default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--modes", default="keyword,semantic,hybrid")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    if args.limit_docs:
        manifest["documents"] = manifest["documents"][: args.limit_docs]
        allowed = {document["id"] for document in manifest["documents"]}
        manifest["queries"] = [query for query in manifest["queries"] if not query["doc"] or query["doc"] in allowed]
    if args.limit_queries:
        manifest["queries"] = manifest["queries"][: args.limit_queries]
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    real = load_real_settings()
    fixture = ModelFixture()
    settings = real.model_copy(update={"generator_base_url": fixture.__enter__().url, "generator_model": "baseline-fixture", "generator_api_key": None})
    runtime = LocalRuntime(model_settings=settings, lease_seconds=900, heartbeat_seconds=30)
    progress = lambda message: print(message, file=sys.stdout, flush=True)
    report: dict[str, Any] = {"configuration": {"retrieval_profile": settings.retrieval_profile, "embedding_model": settings.embedding_model, "embedding_dim": settings.embedding_dim, "chunk_profile": "ragflow-v1", "chunk_tokens": 512, "overlap": 0, "generator": "local fixture (not evaluated)"}, "documents": [], "queries": [], "chunks": []}
    try:
        with runtime:
            client = runtime.client()
            kb = expect(client.post("/api/v1/knowledge-bases", json={"name": f"RAG基线-{started_at}"}), 201)
            kb_id = kb["id"]
            import_result, chunks, records = import_documents(client, kb_id, manifest, args.include_mineru, progress)
            report["documents"] = import_result["documents"]
            report["skipped"] = import_result["skipped"]
            save(out / "chunks.json", list(chunks.values()))
            save(out / "documents.partial.json", report)
            tasks: list[tuple[dict[str, Any], str]] = []
            for query in manifest["queries"]:
                if query["doc"]:
                    record = next((item for item in records if item["id"] == query["doc"]), None)
                    if not record or record["status"] != "succeeded":
                        continue
                for mode in modes:
                    tasks.append((query, mode))
            progress(f"[search] {len(tasks)} calls across {len(modes)} modes")
            # 冷启动会把首次查询的向量与分词延迟计入 P95；先各模式预热一次。
            if manifest["queries"]:
                for mode in modes:
                    search_once(client, kb_id, manifest["queries"][0]["query"], mode, 5, retries=1)
            rows: dict[str, dict[str, Any]] = {query["id"]: {**query, "modes": {}} for query, _ in tasks}
            lock = threading.Lock()
            completed = 0

            def work(task: tuple[dict[str, Any], str]) -> None:
                nonlocal completed
                query, mode = task
                try:
                    hits, error, ms, warnings = search_once(client, kb_id, query["query"], mode, 20)
                    if hits is None:
                        row = {"hits": None, "error": error, "ms": round(ms, 2), "count": 0, "max_score": 0, "warnings": warnings}
                    else:
                        row = {
                            "hits": True,
                            "ms": round(ms, 2),
                            "count": len(hits),
                            "max_score": max((hit["score"] for hit in hits), default=0),
                            "warnings": warnings,
                        }
                        if query["answerable"]:
                            row.update(evaluate_hits(query, hits, chunks))
                        else:
                            row.update({"hit@1": False, "hit@5": False, "hit@20": False, "any@5": False, "mrr": 0.0, "ranked": [{"rank": index, "file": chunks.get(hit["chunk_id"], {}).get("file"), "chunk_id": hit["chunk_id"], "score": hit["score"], "excerpt": (hit.get("excerpt") or "")[:300]} for index, hit in enumerate(hits, 1)]})
                except Exception as exc:  # noqa: BLE001 - 单个查询失败不终止整轮
                    row = {"hits": None, "error": f"{type(exc).__name__}:{str(exc)[:160]}", "ms": 0, "count": 0, "max_score": 0}
                with lock:
                    rows[query["id"]]["modes"][mode] = row
                    completed += 1
                    if completed % 25 == 0:
                        save(out / "queries.partial.json", list(rows.values()))
                        progress(f"[search] {completed}/{len(tasks)}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                list(pool.map(work, tasks))
            report["queries"] = list(rows.values())
            save(out / "queries.json", report["queries"])
            chunking = chunk_statistics(manifest, records, chunks)
            report["chunking"] = chunking
            summary = {
                "started_at": started_at,
                "environment": f"{platform.system()} {platform.release()} / Python {sys.version.split()[0]} / embedded PostgreSQL / real embedding endpoint",
                "configuration": report["configuration"],
                "corpus": {
                    "documents": len(manifest["documents"]),
                    "zh": sum(1 for document in manifest["documents"] if document["lang"] == "zh"),
                    "en": sum(1 for document in manifest["documents"] if document["lang"] == "en"),
                    "queries": len(manifest["queries"]),
                    "positive": sum(1 for query in manifest["queries"] if query["answerable"]),
                    "negative": sum(1 for query in manifest["queries"] if not query["answerable"]),
                },
                "documents_failed": sum(1 for record in records if record["status"] == "failed"),
                "documents_skipped": len(import_result["skipped"]),
                "chunking": chunking,
                "modes": aggregate(report["queries"], modes),
                "breakdown": {},
                "query_errors": sum(1 for row in report["queries"] for mode in modes if row["modes"].get(mode, {}).get("error")),
            }
            for dimension in ("format", "lang", "type"):
                groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for row in report["queries"]:
                    key = row.get(dimension)
                    if key:
                        groups[str(key)].append(row)
                summary["breakdown"][dimension] = {
                    key: {"count": len(value), "modes": aggregate(value, modes)} for key, value in sorted(groups.items())
                }
            report["summary"] = summary
            save(out / "results.json", report)
            save(out / "summary.json", summary)
            write_report(out / "report.md", summary, report)
            print(json.dumps({key: summary[key] for key in ("corpus", "documents_failed", "documents_skipped")}, ensure_ascii=False, indent=2), flush=True)
            print("modes", json.dumps(summary["modes"], ensure_ascii=False, indent=2), flush=True)
    finally:
        fixture.__exit__(None, None, None)


def load_json(path: pathlib.Path, default: Any) -> Any:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def main_phased() -> None:
    """分阶段运行入口：持久库 + 断点续跑，适配单次命令约 3 分钟的执行窗口。"""
    parser = argparse.ArgumentParser()
    parser.add_argument("--include-mineru", action="store_true")
    parser.add_argument("--limit-docs", type=int, default=None)
    parser.add_argument("--limit-queries", type=int, default=None)
    parser.add_argument("--doc-start", type=int, default=0)
    parser.add_argument("--doc-count", type=int, default=None)
    parser.add_argument("--import-only", action="store_true")
    parser.add_argument("--queries-only", action="store_true")
    parser.add_argument("--replace", action="store_true", help="重新导入前删除同标题旧文档及其分块")
    parser.add_argument("--pg-data", default=None)
    parser.add_argument("--storage-root", default=None)
    parser.add_argument("--workers", type=int, default=6)
    parser.add_argument("--modes", default="keyword,semantic,hybrid")
    parser.add_argument("--pdf-parser", choices=("legacy", "auto"), default="legacy", help="PDF 解析路径；默认绕开 MinerU 用本地 pypdf")
    parser.add_argument("--search-engine", choices=("postgresql", "opensearch"), default=None, help="覆盖 .env 的检索组件；默认沿用 KS_SEARCH_ENGINE")
    parser.add_argument("--opensearch-url", default=None)
    parser.add_argument("--opensearch-index", default=None, help="测试专用索引名，避免写入业务索引")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    args = parser.parse_args()
    modes = [mode.strip() for mode in args.modes.split(",") if mode.strip()]
    out = pathlib.Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = load_manifest()
    all_docs = manifest["documents"]
    all_queries = manifest["queries"]
    if args.limit_docs:
        all_docs = all_docs[: args.limit_docs]
        allowed = {document["id"] for document in all_docs}
        all_queries = [query for query in all_queries if not query["doc"] or query["doc"] in allowed]
    if args.limit_queries:
        all_queries = all_queries[: args.limit_queries]

    state = load_json(out / "state.json", {})
    chunks = {item["chunk_id"]: item for item in load_json(out / "chunks.json", [])}
    records = load_json(out / "documents.json", [])
    previous_queries: dict[str, dict[str, Any]] = {}
    for name in ("queries.partial.json", "queries.json"):
        for row in load_json(out / name, []):
            previous_queries[row["id"]] = row

    pg_context = None
    database_url = None
    if args.pg_data:
        import embedded_postgres as ep

        from scripts.local_db import prepare_embedded_env

        pg_directory = pathlib.Path(args.pg_data).resolve()
        pg_directory.mkdir(parents=True, exist_ok=True)
        prepare_embedded_env(str(pg_directory))
        pg_context = ep.get_server(str(pg_directory / "data"))
        server = pg_context.__enter__()
        database_url = server.get_uri().replace("postgresql://", "postgresql+psycopg://")
    storage_root = args.storage_root or str(out / "objects")

    real = load_real_settings()
    fixture = ModelFixture()
    overrides: dict[str, Any] = {"generator_base_url": fixture.__enter__().url, "generator_model": "baseline-fixture", "generator_api_key": None}
    if args.search_engine:
        overrides["search_engine"] = args.search_engine
    if args.opensearch_url:
        overrides["opensearch_url"] = args.opensearch_url
    if args.opensearch_index:
        overrides["opensearch_index"] = args.opensearch_index
    settings = real.model_copy(update=overrides)
    runtime = LocalRuntime(
        database_url=database_url,
        storage_root=storage_root if database_url else None,
        model_settings=settings,
        lease_seconds=900,
        heartbeat_seconds=30,
    )
    progress = lambda message: print(message, file=sys.stdout, flush=True)
    started_at = time.strftime("%Y-%m-%d %H:%M:%S")
    try:
        with runtime:
            client = runtime.client()
            kb_id = state.get("kb_id")
            if kb_id:
                owned = [item["id"] for item in expect(client.get("/api/v1/knowledge-bases"), 200)["items"]]
                if kb_id not in owned:
                    kb_id = None
                    chunks, records, previous_queries = {}, [], {}
            if not args.queries_only:
                if not kb_id:
                    kb_id = expect(client.post("/api/v1/knowledge-bases", json={"name": f"RAG基线-{started_at}"}), 201)["id"]
                    state = {"kb_id": kb_id, "skipped": [], "started_at": started_at}
                    save(out / "state.json", state)
                count = args.doc_count if args.doc_count is not None else len(all_docs) - args.doc_start
                import_docs = all_docs[args.doc_start : args.doc_start + count]
                completed_ids = {record["id"] for record in records if record["status"] == "succeeded"}
                if not args.replace:
                    import_docs = [document for document in import_docs if document["id"] not in completed_ids]
                if import_docs and args.replace:
                    # 重新生成的语料字节已变化；先删除同标题旧文档，避免同一知识库出现新旧两份。
                    wanted = {pathlib.Path(document["file"]).name: document["id"] for document in import_docs}
                    existing = expect(client.get(f"/api/v1/knowledge-bases/{kb_id}/documents"), 200)["items"]
                    removed: set[str] = set()
                    for item in existing:
                        if item["title"] in wanted:
                            client.delete(f"/api/v1/documents/{item['id']}")
                            removed.add(wanted[item["title"]])
                    if removed:
                        records = [record for record in records if record["id"] not in removed]
                        chunks = {key: chunk for key, chunk in chunks.items() if chunk["doc_id"] not in removed}
                        save(out / "chunks.json", list(chunks.values()))
                        save(out / "documents.json", records)
                        progress(f"[replace] removed {len(removed)} old documents")
                if import_docs and args.pdf_parser == "legacy":
                    # 之前默认自动路由的 PDF 已作为失败文档存在；删除后重传才能改走本地解析。
                    wanted = {pathlib.Path(document["file"]).name for document in import_docs if document["format"] == "pdf"}
                    existing = expect(client.get(f"/api/v1/knowledge-bases/{kb_id}/documents"), 200)["items"]
                    for item in existing:
                        if item["title"] in wanted and item["status"] != "active":
                            client.delete(f"/api/v1/documents/{item['id']}")
                if import_docs:
                    result, new_chunks, new_records = import_documents(client, kb_id, {"documents": import_docs}, args.include_mineru, progress, pdf_parser=args.pdf_parser)
                    chunks.update(new_chunks)
                    merged_records = {record["id"]: record for record in records}
                    merged_records.update({record["id"]: record for record in new_records})
                    records = list(merged_records.values())
                    state.setdefault("skipped", []).extend(result["skipped"])
                    save(out / "chunks.json", list(chunks.values()))
                    save(out / "documents.json", records)
                    save(out / "state.json", state)
                    progress(f"[import] docs={len(records)} chunks={len(chunks)} skipped={len(state.get('skipped', []))}")
            if args.import_only:
                print(json.dumps({"docs": len(records), "chunks": len(chunks), "skipped": len(state.get("skipped", []))}, ensure_ascii=False), flush=True)
                return

            if not kb_id:
                raise SystemExit("没有可用的知识库；请先运行一次导入阶段")
            rows: dict[str, dict[str, Any]] = {}
            for query in all_queries:
                prior = previous_queries.get(query["id"], {})
                rows[query["id"]] = {**query, "modes": dict(prior.get("modes", {}))}
            available_docs = {chunk["doc_id"] for chunk in chunks.values()}
            tasks: list[tuple[dict[str, Any], str]] = []
            for query in all_queries:
                if query["doc"] and query["doc"] not in available_docs:
                    continue
                for mode in modes:
                    if rows[query["id"]]["modes"].get(mode, {}).get("hits") is not None:
                        continue
                    tasks.append((query, mode))
            progress(f"[search] {len(tasks)} pending calls across {len(modes)} modes")
            if tasks:
                warm_query = next((query["query"] for query in all_queries if not query["doc"] or query["doc"] in available_docs), "基线预热")
                for mode in modes:
                    search_once(client, kb_id, warm_query, mode, 5, retries=1)
            lock = threading.Lock()
            completed = 0

            def work(task: tuple[dict[str, Any], str]) -> None:
                nonlocal completed
                query, mode = task
                try:
                    hits, error, ms, warnings = search_once(client, kb_id, query["query"], mode, 20)
                    if hits is None:
                        row = {"hits": None, "error": error, "ms": round(ms, 2), "count": 0, "max_score": 0, "warnings": warnings}
                    else:
                        row = {"hits": True, "ms": round(ms, 2), "count": len(hits), "max_score": max((hit["score"] for hit in hits), default=0), "warnings": warnings}
                        if query["answerable"]:
                            row.update(evaluate_hits(query, hits, chunks))
                        else:
                            row.update({"hit@1": False, "hit@5": False, "hit@20": False, "any@5": False, "mrr": 0.0, "ranked": [{"rank": index, "file": chunks.get(hit["chunk_id"], {}).get("file"), "chunk_id": hit["chunk_id"], "score": hit["score"], "excerpt": (hit.get("excerpt") or "")[:300]} for index, hit in enumerate(hits, 1)]})
                except Exception as exc:  # noqa: BLE001 - 单个查询失败不终止整轮
                    row = {"hits": None, "error": f"{type(exc).__name__}:{str(exc)[:160]}", "ms": 0, "count": 0, "max_score": 0, "warnings": []}
                with lock:
                    rows[query["id"]]["modes"][mode] = row
                    completed += 1
                    if completed % 20 == 0:
                        ordered = [rows[query["id"]] for query in all_queries if query["id"] in rows]
                        save(out / "queries.partial.json", ordered)
                        progress(f"[search] {completed}/{len(tasks)}")

            with concurrent.futures.ThreadPoolExecutor(max_workers=max(1, args.workers)) as pool:
                list(pool.map(work, tasks))
            ordered_rows = [rows[query["id"]] for query in all_queries if query["id"] in rows]
            save(out / "queries.json", ordered_rows)
            chunking = chunk_statistics({"documents": all_docs}, records, chunks)
            summary = {
                "started_at": state.get("started_at", started_at),
                "environment": f"{platform.system()} {platform.release()} / Python {sys.version.split()[0]} / embedded PostgreSQL / real embedding endpoint",
                "configuration": {"retrieval_profile": settings.retrieval_profile, "embedding_model": settings.embedding_model, "embedding_dim": settings.embedding_dim, "chunk_profile": "ragflow-v1", "chunk_tokens": 512, "overlap": 0, "generator": "local fixture (not evaluated)"},
                "corpus": {"documents": len(all_docs), "zh": sum(1 for document in all_docs if document["lang"] == "zh"), "en": sum(1 for document in all_docs if document["lang"] == "en"), "queries": len(all_queries), "positive": sum(1 for query in all_queries if query["answerable"]), "negative": sum(1 for query in all_queries if not query["answerable"])},
                "documents_failed": sum(1 for record in records if record["status"] == "failed"),
                "documents_skipped": sum(
                    1 for document in all_docs
                    if document["requires_mineru"] and document["id"] not in {record["id"] for record in records}
                ),
                "ocr_documents": sum(1 for record in records if record.get("parser_profile") == "mineru-api4.1-v1"),
                "ocr_chunks": sum(record.get("chunk_count", 0) for record in records if record.get("parser_profile") == "mineru-api4.1-v1"),
                "workers": args.workers,
                "pdf_parser": args.pdf_parser,
                "search_engine": settings.search_engine,
                "opensearch": (
                    {"url": settings.opensearch_url, "index": settings.opensearch_index}
                    if settings.search_engine == "opensearch" else None
                ),
                "chunking": chunking,
                "modes": aggregate(ordered_rows, modes),
                "breakdown": {},
                "query_errors": sum(1 for row in ordered_rows for mode in modes if row["modes"].get(mode, {}).get("error")),
            }
            for dimension in ("format", "lang", "type"):
                groups: dict[str, list[dict[str, Any]]] = defaultdict(list)
                for row in ordered_rows:
                    key = row.get(dimension)
                    if key:
                        groups[str(key)].append(row)
                summary["breakdown"][dimension] = {key: {"count": len(value), "modes": aggregate(value, modes)} for key, value in sorted(groups.items())}
            attempted_ids = {record["id"] for record in records}
            skipped_list = [
                {"id": document["id"], "file": document["file"], "reason": "requires MinerU (images/scanned or multi-column PDF)"}
                for document in all_docs
                if document["requires_mineru"] and document["id"] not in attempted_ids
            ]
            report = {"configuration": summary["configuration"], "documents": records, "skipped": skipped_list, "queries": ordered_rows, "summary": summary}
            save(out / "results.json", report)
            save(out / "summary.json", summary)
            write_report(out / "report.md", summary, report)
            print(json.dumps({key: summary[key] for key in ("corpus", "documents_failed", "documents_skipped")}, ensure_ascii=False, indent=2), flush=True)
            print("modes", json.dumps(summary["modes"], ensure_ascii=False, indent=2), flush=True)
    finally:
        try:
            fixture.__exit__(None, None, None)
        finally:
            if pg_context is not None:
                pg_context.__exit__(None, None, None)


if __name__ == "__main__":
    main_phased()
