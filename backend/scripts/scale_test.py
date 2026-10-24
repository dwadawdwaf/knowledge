"""规模压测：批量合成文档的入库吞吐与检索延迟（实测，不外推承诺）。

在测试库上生成 N 份合成制度文档（含可检索锚点条款），走完整上传→处理链路，
再测检索延迟分位数。默认用 HashEmbedder 测链路机制（快）；--real 用 .env 配置的
真实向量端点（bge-m3，CPU 下入库耗时按量级增长，先用小 N 试）。

    KS_DATABASE_URL=$(cat .data/db_test_url) python scripts/scale_test.py --docs 300
    KS_DATABASE_URL=$(cat .data/db_test_url) python scripts/scale_test.py --docs 20 --real
"""

from __future__ import annotations

import argparse
import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from knowledge.adapters.embedding import HashEmbedder, build_embedder
from knowledge.adapters.parser import build_parser_registry
from knowledge.adapters.storage import LocalStorage
from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.repositories.tables import Base
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import retrieval
from knowledge.worker.loop import WorkerDeps, process_next

DEPARTMENTS = ["综合部", "财务部", "信息科", "行政部", "采购部", "质检部", "安监部", "后勤部"]
TOPICS = ["档案管理", "印章使用", "值班管理", "物资领用", "访客接待", "会议管理", "车辆调度", "机房管理"]


def _synth_doc(index: int) -> tuple[str, bytes]:
    department = DEPARTMENTS[index % len(DEPARTMENTS)]
    topic = TOPICS[index % len(TOPICS)]
    lines = [f"# {department}{topic}细则（合成压测资料 {index}）", ""]
    for clause in range(1, 13):
        if clause == 7:
            lines.append(f"第七条 压测锚点：{topic}专项事务由{department}牵头，编号 SCALE-{index:04d}。")
        else:
            lines.append(
                f"第{['一','二','三','四','五','六','七','八','九','十','十一','十二'][clause-1]}条 "
                f"{topic}事项{clause}：按制度{clause}号执行，由{department}备案，期限{clause * 3}个工作日。"
            )
        lines.append("")
    return f"压测-{department}{topic}-{index:04d}.md", "\n".join(lines).encode("utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="规模压测（合成数据）")
    parser.add_argument("--docs", type=int, default=300, help="合成文档数量")
    parser.add_argument("--real", action="store_true", help="使用 .env 配置的真实向量端点")
    args = parser.parse_args()

    overrides: dict[str, object] = {"database_url": os.environ.get("KS_DATABASE_URL") or (ROOT / ".data" / "db_test_url").read_text(encoding="utf-8").strip()}
    if not os.environ.get("KS_STORAGE_ROOT"):
        overrides["storage_root"] = tempfile.mkdtemp(prefix="ks_scale_")
    settings = Settings().model_copy(update=overrides)

    engine = create_engine(settings.database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    try:
        with Session(engine, expire_on_commit=False) as session:
            actor = Actor(subject_id="scale", tenant_id="default")
            kb = knowledge_service.create(session, actor, "压测库")
            embedder = build_embedder(settings) if args.real else HashEmbedder(dimension=settings.embedding_dim)
            deps = WorkerDeps(
                settings=settings, storage=LocalStorage(settings.storage_root),
                registry=build_parser_registry(),
                embedder=embedder,
            )
            print(f"[scale] embedder={deps.embedder.model} docs={args.docs}")

            started = time.perf_counter()
            for index in range(args.docs):
                filename, data = _synth_doc(index)
                document_service.upload(
                    session, actor, settings, deps.storage, deps.registry,
                    kb_id=kb.id, filename=filename, data=data,
                    embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
                )
            upload_seconds = time.perf_counter() - started
            print(f"[scale] 上传 {args.docs} 份：{upload_seconds:.1f}s（{args.docs / upload_seconds:.0f} 份/s）")

            started = time.perf_counter()
            processed = 0
            while process_next(session, deps):
                processed += 1
            ingest_seconds = time.perf_counter() - started
            chunks = session.execute(text("SELECT count(*) FROM chunks")).scalar()
            print(f"[scale] 处理 {processed} 个任务：{ingest_seconds:.1f}s，"
                  f"共 {chunks} 分块（{int(chunks) / ingest_seconds:.0f} 块/s，含解析/向量/发布）")

            # 检索延迟：锚点查询（应命中）与普通查询（排名竞争）
            queries = [f"压测锚点 编号 SCALE-{i:04d}" for i in range(0, args.docs, max(1, args.docs // 30))]
            latencies: list[float] = []
            hits_found = 0
            for query in queries:
                began = time.perf_counter()
                hits = retrieval.search(session, actor, settings, deps.embedder, query=query, kb_ids=[kb.id])
                latencies.append((time.perf_counter() - began) * 1000)
                hits_found += bool(hits)
            latencies.sort()
            p50 = statistics.median(latencies)
            p95 = latencies[int(len(latencies) * 0.95) - 1] if len(latencies) >= 20 else latencies[-1]
            print(f"[scale] 检索 {len(queries)} 次：命中 {hits_found}，"
                  f"p50={p50:.0f}ms p95={p95:.0f}ms max={latencies[-1]:.0f}ms")

            # 锚点精确性抽查：任取一锚点，命中块应含该编号
            probe = f"压测锚点 编号 SCALE-{args.docs // 2:04d}"
            hits = retrieval.search(session, actor, settings, deps.embedder, query=probe, kb_ids=[kb.id])
            assert hits and f"SCALE-{args.docs // 2:04d}" in hits[0].text, "锚点查询未命中目标条款"
            print("[scale] 锚点精确性抽查通过")
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
