"""第二轮公开长文档实验；只接受已核验本地语料，保留旧库，不自动重跑。"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowledge.adapters.parser import build_parser_registry, detect_media_type
from knowledge.config import Settings
from knowledge.domain.chunking import chunk_blocks
from knowledge.domain.errors import DomainError
from scripts import local_db
from scripts.local_runtime import BACKEND, WORKBENCH_OBJECTS, LocalRuntime
from scripts.real_model import load_real_settings
from scripts.retrieval_baseline import (
    Case,
    Dataset,
    ExperimentSpec,
    Json,
    Source,
    normalize_text,
    run_experiment,
    say,
    supports_fact,
)

ROOT = BACKEND / ".data" / "public-corpus-v1"
OUTPUT = BACKEND / ".data" / "public-round2-mineru"
PROFILE = "mineru-api4.1-v1"
PUBLIC_IDS = {
    "plan": "ndrc-14th-plan-2021",
    "wb": "worldbank-wdr2024-overview-zh",
    "rmf": "nist-ai-rmf-100-1-2023",
    "gai": "nist-genai-profile-600-1-2024",
    "nasa": "nasa-se-handbook-rev2",
    "law": "civil-code-2020-official",
}
REPORT_FILE = "retrieval-evaluation-v2-public.txt"


def contained_file(root: Path, relative: str) -> Path:
    """标注和清单不可将读取范围扩大到公开语料目录之外。"""
    path = (root / relative).resolve()
    if not path.is_relative_to(root.resolve()) or not path.is_file():
        raise ValueError("公开语料文件越界或缺失")
    return path


def prepare(root: Path, settings: Settings) -> tuple[Dataset, dict[str, bytes], Json, str]:
    """复用生产解析器/分块参数本地预检；不以非空文本代替金标可见性检查。"""
    manifest_raw = contained_file(root, "manifest.json").read_bytes()
    labels_raw = contained_file(root, "cases-v2.json").read_bytes()
    manifest = json.loads(manifest_raw)
    labels = json.loads(labels_raw)
    original_pages = json.loads(contained_file(root, "extracted-pages.json").read_bytes())
    entries = {entry["id"]: entry for entry in manifest["sources"]}
    if set(entries) != set(PUBLIC_IDS.values()):
        raise ValueError("公开语料不是已批准的六份资料")
    cases = [Case.model_validate(case) for case in labels["cases"]]
    if len(cases) != 48 or any(
        sum(case.partition == part for case in cases) != 24 for part in ("development", "holdout")
    ):
        raise ValueError("第二轮必须是固定24+24题")
    facts_by_partition = {
        part: {
            normalize_text(fact.quote)
            for case in cases
            if case.partition == part
            for fact in case.facts
        }
        for part in ("development", "holdout")
    }
    if facts_by_partition["development"] & facts_by_partition["holdout"]:
        raise ValueError("开发与留出集不得复用相同标注事实")
    registry = build_parser_registry(settings)
    sources: list[Source] = []
    payloads: dict[str, bytes] = {}
    preparation: Json = {
        "corpus_kind": "public",
        "parser_profile": PROFILE,
        "parse_output_bytes": settings.parse_output_bytes,
        "mineru_timeout_seconds": settings.mineru_timeout_seconds,
        "split_note": labels["split_note"],
        "parsing": [],
        "fact_presence": {},
        "source_manifest_sha256": hashlib.sha256(manifest_raw).hexdigest(),
    }
    fingerprint = hashlib.sha256(labels_raw + PROFILE.encode())
    for key, identifier in PUBLIC_IDS.items():
        entry = entries[identifier]
        original = contained_file(root, entry["local_file"])
        original_bytes = original.read_bytes()
        if hashlib.sha256(original_bytes).hexdigest() != entry["sha256"]:
            raise ValueError("公开原件摘要不符")
        path = (
            contained_file(root, entry["inspection"]["derived_file"]) if key == "law" else original
        )
        data = path.read_bytes()
        fingerprint.update(hashlib.sha256(data).digest())
        name = identifier + path.suffix
        source = Source(key=key, name=name)
        payloads[key] = data
        say(f"[解析预检] source={key}，bytes={len(data)}，profile={PROFILE}")
        started = time.monotonic()
        media_type = detect_media_type(data, name)
        blocks = registry.for_file(media_type, name, profile=PROFILE).parse(data, media_type, name)
        chunks = chunk_blocks(blocks, settings.chunk_max_chars, settings.chunk_overlap_chars)
        # 金标以原件盘点文本为准；识别差异单独计入可见性，不改金标迁就解析结果。
        source.text = "\n".join(page["text"] for page in original_pages[identifier]["pages"])
        sources.append(source)
        page_count = entry["inspection"]["page_count"]
        parsed_pages = {block.locator.page for block in blocks if block.locator.page is not None}
        preparation["parsing"].append(
            {
                "source": key,
                "source_id": identifier,
                "title": entry["title"],
                "source_url": entry["source_page"],
                "version": entry["version"],
                "sha256": entry["sha256"],
                "input_pages": page_count,
                "parsed_pages": len(parsed_pages) if page_count else None,
                "unparsed_pages": sorted(set(range(1, page_count + 1)) - parsed_pages)
                if page_count
                else [],
                "chunks": len(chunks),
                "chars": sum(len(block.text) for block in blocks),
                "elapsed_ms": round((time.monotonic() - started) * 1000),
                "derivation": "official_html_to_txt" if key == "law" else "original_pdf",
                "warnings": sorted({warning for block in blocks for warning in block.warnings}),
            }
        )
        for case in cases:
            for index, fact in enumerate(case.facts):
                if fact.document == key:
                    present = any(
                        supports_fact(fact, chunk.text, chunk.locator) for chunk in chunks
                    )
                    preparation["fact_presence"].setdefault(case.id, {})[str(index)] = present
        say(
            f"[解析预检] source={key}，blocks={len(blocks)}，chunks={len(chunks)}，pages={len(parsed_pages)}"
        )
    dataset = Dataset(revision=labels["revision"], documents=sources, cases=cases)
    return dataset, payloads, preparation, fingerprint.hexdigest()


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--confirm-public", action="store_true", help="确认只将本批官方公开片段发送到已配置真实模型"
    )
    parser.add_argument(
        "--prepare-only", action="store_true", help="仅本地预检，不入库、不调用模型"
    )
    parser.add_argument("--serve", action="store_true")
    parser.add_argument(
        "--resume-ingestion", action="store_true", help="显式恢复尚未进入问答的入库失败轮次"
    )
    parser.add_argument("--port", type=int, default=8013)
    args = parser.parse_args()
    if not args.prepare_only and not args.confirm_public:
        say("未确认公开资料的真实模型范围，未入库、未调用模型。")
        return 1
    marker = OUTPUT / "run-started.json"
    recovery = OUTPUT / "ingestion-recovery-started.json"
    if args.resume_ingestion and (
        args.prepare_only
        or not marker.exists()
        or recovery.exists()
        or (OUTPUT / "report.json").exists()
    ):
        raise ValueError("只允许显式恢复一次尚无报告的入库失败轮次")
    if not args.prepare_only and marker.exists() and not args.resume_ingestion:
        say("本轮已启动过，不自动重复导入或重跑。请先核对已有报告。")
        return 1
    settings = load_real_settings()
    if (
        urlsplit(settings.embedding_base_url or "").hostname
        not in {"127.0.0.1", "localhost", "::1"}
        or settings.embedding_model != "bge-m3"
        or settings.generator_model != "deepseek-flash"
    ):
        raise ValueError("模型配置超出本轮批准范围，不启动实验")
    dataset, payloads, preparation, digest = prepare(ROOT, settings)
    if args.resume_ingestion and json.loads(marker.read_bytes())["dataset_sha256"] != digest:
        raise ValueError("语料或标注指纹变化，不恢复原轮次")
    OUTPUT.mkdir(parents=True, exist_ok=True)
    (OUTPUT / "preflight.json").write_text(
        json.dumps(preparation, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    say(f"[冻结] 48题（开发24/留出24）；dataset_sha256={digest}")
    if args.prepare_only:
        say("本地预检完成，未入库，未调用外部模型。")
        return 0
    (recovery if args.resume_ingestion else marker).write_text(
        json.dumps(
            {
                "started_at": datetime.now(UTC).isoformat(),
                "dataset_sha256": digest,
                "request_limit": 240,
            }
        ),
        encoding="utf-8",
    )
    url = local_db.DB_URL_FILE.read_text(encoding="utf-8").strip()
    spec = ExperimentSpec(
        kb_name="第二轮检索评测 · 官方公开长文档 · MinerU",
        report_file=REPORT_FILE,
        request_limit=240,
        add_noise=False,
        publish_every=12,
        upload_timeout=900,
        title="第二轮 · 真实长文档",
        dataset_sha256=digest,
        parser_profile=PROFILE,
        compact_report=True,
    )
    with LocalRuntime(
        database_url=url,
        storage_root=str(WORKBENCH_OBJECTS),
        model_settings=settings,
        port=args.port,
    ) as runtime:
        report = run_experiment(
            runtime,
            dataset,
            report_directory=OUTPUT,
            spec=spec,
            source_loader=lambda source: payloads[source.key],
            preparation=preparation,
            resume_ingestion=args.resume_ingestion,
        )
        say(
            f"[第二轮完成] cases={len(report['cases'])} requests={report['requests']}/240；旧库与第一轮报告保留。"
        )
        if args.serve:
            say("前端保持运行，Ctrl+C停止；不重新执行实验。")
            try:
                while True:
                    time.sleep(60)
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("实验中止，不自动重跑，已有数据保留。")
        raise SystemExit(130) from None
    except (ValueError, RuntimeError, OSError, AssertionError, DomainError) as exc:
        say(f"第二轮未完成，错误类型={type(exc).__name__}；不输出正文或密钥，不自动重跑。")
        raise SystemExit(1) from None
