"""隔离库内对照旧检索、优化检索和结构分块；仅自制集与哈希向量，不调用外部模型。"""

from __future__ import annotations

import argparse
import hashlib
import json
import statistics
import subprocess
import sys
import tempfile
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BACKEND = Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session

from knowledge.adapters.embedding import HashEmbedder
from knowledge.adapters.parser import build_parser_registry
from knowledge.adapters.storage import LocalStorage
from knowledge.config import Settings
from knowledge.domain.entities import Actor, ParsedBlock, SearchHit
from knowledge.repositories.tables import Base, Chunk
from knowledge.services import document as documents
from knowledge.services import knowledge as knowledge_bases
from knowledge.services import retrieval
from knowledge.worker.loop import WorkerDeps, process_next
from scripts.local_runtime import isolated_database
from scripts.retrieval_baseline import (
    DATASET,
    Case,
    Dataset,
    Fact,
    contains_quote,
    noise_documents,
    normalize_text,
    retrieval_metrics,
    source_bytes,
    supports_fact,
)

ARMS = (
    ("baseline-v1", "legacy-v1", "baseline-v1"),
    ("retrieval-v2", "legacy-v1", "optimized-v2"),
    ("structure-v2", "structure-v2", "optimized-v2"),
)


class CachedParser:
    """各方案复用同一次解析，分块差异不混入解析差异。"""

    def __init__(self, blocks: list[ParsedBlock]) -> None:
        self.blocks = blocks

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        return list(self.blocks)


class CachedRegistry:
    def __init__(self, blocks: dict[str, list[ParsedBlock]]) -> None:
        self.blocks = blocks

    def for_file(
        self, media_type: str, filename: str, *, profile: str | None = None
    ) -> CachedParser:
        return CachedParser(self.blocks[filename])


def metrics(case: Case, hits: list[SearchHit], sources: dict[str, dict]) -> dict:
    return retrieval_metrics(
        case,
        [
            {
                "document_id": hit.document_id,
                "build_id": hit.build_id,
                "excerpt": hit.text,
                "locator": hit.locator,
            }
            for hit in hits
        ],
        sources,
    )


def provenance_supports(fact: Fact, text: str, locator: dict) -> bool:
    """新增指标：匹配文字必须落在正确的原页来源中；历史严格指标保持不变。"""
    if not locator.get("sources"):
        return supports_fact(fact, text, locator)
    if not contains_quote(fact, text):
        return False
    if not fact.pdf_pages:
        return True
    searchable = normalize_text(text) if fact.normalized else text
    quote = normalize_text(fact.quote) if fact.normalized else fact.quote
    start = searchable.find(quote)
    while start >= 0:
        end = start + len(quote)
        for source in locator["sources"]:
            left, right = source["chunk_start"], source["chunk_end"]
            if fact.normalized:
                left, right = len(normalize_text(text[:left])), len(normalize_text(text[:right]))
            if left < end and right > start and source["locator"].get("page") in fact.pdf_pages:
                return True
        start = searchable.find(quote, start + 1)
    return False


def provenance_metrics(case: Case, hits: list[SearchHit], sources: dict[str, dict]) -> dict:
    if not case.facts:
        return {"fact_recall": None, "all_facts_found": None}
    found = [
        any(
            hit.document_id == sources[fact.document]["document_id"]
            and hit.build_id == sources[fact.document]["build_id"]
            and provenance_supports(fact, hit.text, hit.locator)
            for hit in hits
        )
        for fact in case.facts
    ]
    return {"fact_recall": sum(found) / len(found), "all_facts_found": all(found)}


def freeze_baseline() -> dict:
    root = BACKEND.parent
    paths = (
        BACKEND / "testdata/retrieval_baseline/results/2026-10-30/report.json",
        BACKEND / ".data/public-round2-mineru/report.json",
    )
    snapshots = []
    for path in paths:
        item: dict[str, Any] = {"path": str(path.relative_to(root)), "available": path.is_file()}
        if path.is_file():
            item["sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
        snapshots.append(item)
    return {
        "commit": subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=root, text=True
        ).strip(),
        "working_tree_diff_sha256": hashlib.sha256(
            subprocess.check_output(["git", "diff", "HEAD"], cwd=root)
        ).hexdigest(),
        "historical_reports": snapshots,
        "dataset_sha256": hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "implementation_sha256": {
            str(path.relative_to(BACKEND)): hashlib.sha256(path.read_bytes()).hexdigest()
            for directory in (BACKEND / "knowledge", BACKEND / "scripts")
            for path in sorted(directory.rglob("*.py"))
            if ".data" not in path.parts
        },
        "historical_results_reference": "issues.md：当前真实检索基线；本工具不改写历史分数",
        "scope": "synthetic-hash-only; historical real-model scores are not comparable",
    }


def run_comparison(progress: Callable[[str], None] = print) -> dict:
    dataset = Dataset.model_validate_json(DATASET.read_bytes())
    # 仅使用代码默认值，不读取 .env 或环境中的真实服务配置。
    settings = Settings.model_construct(
        document_parser_profile="legacy-v1", pdf_parser_profile="legacy-v1"
    )
    registry = build_parser_registry(settings)
    payloads = {source.name: source_bytes(source) for source in dataset.documents}
    payloads.update(noise_documents())
    blocks = {}
    from knowledge.adapters.parser import detect_media_type

    for name, data in payloads.items():
        media = detect_media_type(data, name)
        blocks[name] = registry.for_file(media, name, profile="legacy-v1").parse(data, media, name)
    report: dict[str, Any] = {
        "revision": "retrieval-optimization-engineering-v1",
        "created_at": datetime.now(UTC).isoformat(),
        "manifest": freeze_baseline(),
        "arms": [],
        "external_model_calls": 0,
    }
    report["manifest"]["original_sha256"] = {
        name: hashlib.sha256(data).hexdigest() for name, data in payloads.items()
    }
    with (
        tempfile.TemporaryDirectory(prefix="ks_compare_objects_") as storage,
        isolated_database(progress) as url,
    ):
        engine = create_engine(url)
        try:
            with engine.begin() as conn:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
            Base.metadata.create_all(engine)
            with Session(engine, expire_on_commit=False) as session:
                for name, strategy, profile in ARMS:
                    progress(f"[compare] {name}")
                    configured = settings.model_copy(
                        update={
                            "chunk_strategy": strategy,
                            "retrieval_profile": profile,
                            "storage_root": storage,
                        }
                    )
                    deps = WorkerDeps(
                        configured, LocalStorage(storage), CachedRegistry(blocks), HashEmbedder()
                    )
                    actor = Actor("optimization-compare", "default")
                    kb = knowledge_bases.create(session, actor, name)
                    sources = {}
                    build_ids = []
                    for source in dataset.documents:
                        ack = documents.upload(
                            session,
                            actor,
                            configured,
                            deps.storage,
                            deps.registry,
                            kb_id=kb.id,
                            filename=source.name,
                            data=payloads[source.name],
                            embedder_model=deps.embedder.model,
                            embedder_dim=deps.embedder.dimension,
                        )
                        sources[source.key] = {
                            "document_id": str(ack.document_id),
                            "build_id": str(ack.build_id),
                        }
                        build_ids.append(ack.build_id)
                    for filename, data in noise_documents().items():
                        ack = documents.upload(
                            session,
                            actor,
                            configured,
                            deps.storage,
                            deps.registry,
                            kb_id=kb.id,
                            filename=filename,
                            data=data,
                            embedder_model=deps.embedder.model,
                            embedder_dim=deps.embedder.dimension,
                        )
                        build_ids.append(ack.build_id)
                    while process_next(session, deps):
                        pass
                    records: list[dict[str, Any]] = []
                    for case in dataset.cases:
                        variants = {}
                        evidence_metrics = None
                        for mode, count in (
                            ("keyword", 5),
                            ("semantic", 5),
                            ("hybrid", 5),
                            ("hybrid", 20),
                        ):
                            import time

                            started = time.monotonic()
                            hits = retrieval.search(
                                session,
                                actor,
                                configured,
                                deps.embedder,
                                query=case.question,
                                kb_ids=[kb.id],
                                mode=mode,
                                top_k=count,
                            )
                            variants[f"{mode}_{count}"] = {
                                **metrics(case, hits, sources),
                                "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                                "hit_count": len(hits),
                                "provenance": provenance_metrics(case, hits, sources),
                                "candidates": [
                                    {
                                        "chunk_id": hit.chunk_id,
                                        "score": hit.score,
                                        **hit.diagnostics,
                                    }
                                    for hit in hits
                                ],
                            }
                            if mode == "hybrid" and count == 20:
                                evidence = retrieval.select_evidence(
                                    session, actor, configured, [kb.id], hits
                                )
                                evidence_metrics = {
                                    **metrics(case, evidence, sources),
                                    "provenance": provenance_metrics(case, evidence, sources),
                                    "items": len(evidence),
                                    "chars": sum(len(hit.text) for hit in evidence),
                                }
                        records.append(
                            {
                                "id": case.id,
                                "category": case.category,
                                "answerable": bool(case.facts),
                                "retrieval": variants,
                                "evidence": evidence_metrics,
                            }
                        )
                    answerable = [item for item in records if item["answerable"]]
                    summary = {}
                    for variant in ("keyword_5", "semantic_5", "hybrid_5", "hybrid_20"):
                        summary[variant] = {
                            "mean_fact_recall": statistics.mean(
                                item["retrieval"][variant]["fact_recall"] for item in answerable
                            ),
                            "complete": sum(
                                item["retrieval"][variant]["all_facts_found"] for item in answerable
                            ),
                            "answerable": len(answerable),
                            "negative_candidate_returns": sum(
                                not item["answerable"]
                                and item["retrieval"][variant]["hit_count"] > 0
                                for item in records
                            ),
                        }
                    report["arms"].append(
                        {
                            "name": name,
                            "chunk_strategy": strategy,
                            "retrieval_profile": profile,
                            "settings": {
                                key: getattr(configured, key)
                                for key in (
                                    "chunk_max_chars",
                                    "chunk_overlap_chars",
                                    "similarity_floor",
                                    "semantic_max_distance",
                                    "recall_limit",
                                    "rrf_k",
                                    "keyword_weight",
                            "keyword_local_floor",
                                    "semantic_weight",
                                    "evidence_max_items",
                                    "evidence_max_chars",
                                )
                            },
                            "chunk_count": session.scalar(
                                select(func.count())
                                .select_from(Chunk)
                                .where(Chunk.build_id.in_(build_ids))
                            ),
                            "summary": summary,
                            "cases": records,
                        }
                    )
        finally:
            engine.dispose()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--output", type=Path, default=BACKEND / ".data/retrieval-optimization/comparison.json"
    )
    args = parser.parse_args()
    if args.output.exists():
        parser.error("输出已存在，请选择新文件名，保留旧实验")
    report = run_comparison()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open("x", encoding="utf-8") as output:
        json.dump(report, output, ensure_ascii=False, indent=2)
    print(f"离线对照已保存：{args.output}；外部模型调用为0，不代表真实模型效果。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
