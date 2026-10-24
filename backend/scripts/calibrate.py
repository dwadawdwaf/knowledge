"""阈值标定工具：用评测语料实测并推荐 KS_SIMILARITY_FLOOR 与 KS_SEMANTIC_MAX_DISTANCE。

在测试库上重建语料并逐一测量「命中用例 vs 语料外问题」在关键词路与语义路的分布，
给出两阈值的推荐值与重叠告警。配置真实模型端点后运行（design §6.1：换向量模型必须重标）：

    export KS_DATABASE_URL=$(cat .data/db_test_url)
    KS_EMBEDDING_BASE_URL=... KS_EMBEDDING_API_KEY=... \
    KS_EMBEDDING_MODEL=bge-m3 python scripts/calibrate.py

未配置端点时使用 HashEmbedder，结果仅用于验证工具与回归对照。
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.repositories.tables import Base
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import retrieval
from knowledge.worker.loop import WorkerDeps, build_deps, process_next
from testdata.corpus.generate import make_docx, make_pdf, pdf_supported
from testdata.corpus.labels import CORPUS_FILES, HIT_CASES, NEGATIVE_QUESTIONS

CORPUS_ROOT = ROOT / "testdata" / "corpus"


def _database_url(settings: Settings) -> str:
    if os.environ.get("KS_DATABASE_URL"):
        return settings.database_url
    fallback = ROOT / ".data" / "db_test_url"
    if fallback.is_file():
        return fallback.read_text(encoding="utf-8").strip()
    return settings.database_url


def _build_corpus(session: Session, deps: WorkerDeps) -> uuid.UUID:
    actor = Actor(subject_id="calibrate", tenant_id="default")
    kb = knowledge_service.create(session, actor, "标定库")

    def _upload(filename: str, data: bytes) -> None:
        document_service.upload(
            session, actor, deps.settings, deps.storage, deps.registry,
            kb_id=kb.id, filename=filename, data=data,
            embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
        )

    for filename in CORPUS_FILES.values():
        _upload(filename, (CORPUS_ROOT / filename).read_bytes())
    _upload("数据备份管理细则.docx", make_docx())
    if pdf_supported():
        _upload("访客管理规程.pdf", make_pdf())

    while process_next(session, deps):
        pass
    return kb.id


def _matching_similarity(session: Session, question: str, snippets: list[str]) -> float:
    """含预期片段的分块与问题的 trigram 相似度（关键词路）。"""
    cond = " OR ".join(f"c.tsv LIKE :s{i}" for i in range(len(snippets)))
    params: dict[str, object] = {"q": question}
    params.update({f"s{i}": f"%{sn}%" for i, sn in enumerate(snippets)})
    row = session.execute(
        text(f"SELECT max(similarity(c.tsv, :q)) FROM chunks c WHERE {cond}"), params
    ).scalar()
    return float(row or 0.0)


def _matching_distance(session: Session, question: str, vector: list[float], snippets: list[str]) -> float:
    """含预期片段的分块与查询向量的余弦距离（语义路）。"""
    cond = " OR ".join(f"c.tsv LIKE :s{i}" for i in range(len(snippets)))
    vec = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
    params: dict[str, object] = {"q": question, "vec": vec}
    params.update({f"s{i}": f"%{sn}%" for i, sn in enumerate(snippets)})
    row = session.execute(
        text(
            f"SELECT min(c.embedding <=> CAST(:vec AS vector)) FROM chunks c WHERE {cond}"
        ),
        params,
    ).scalar()
    return float(row or 1.0)


def _corpus_extreme(session: Session, question: str, vector: list[float]) -> tuple[float, float]:
    """全库最大 trigram 相似度与最小余弦距离（负面问题的最坏情况）。"""
    vec = "[" + ",".join(f"{value:.6f}" for value in vector) + "]"
    sim = float(
        session.execute(
            text("SELECT max(similarity(c.tsv, :q)) FROM chunks c"), {"q": question}
        ).scalar()
        or 0.0
    )
    dist = float(
        session.execute(
            text("SELECT min(c.embedding <=> CAST(:vec AS vector)) FROM chunks c"),
            {"vec": vec},
        ).scalar()
        or 1.0
    )
    return sim, dist


def main() -> int:
    overrides: dict[str, object] = {"database_url": _database_url(Settings())}
    if not os.environ.get("KS_STORAGE_ROOT"):
        overrides["storage_root"] = tempfile.mkdtemp(prefix="ks_calibrate_")
    settings = Settings().model_copy(update=overrides)
    deps = build_deps(settings)
    print(f"[calibrate] embedder = {deps.embedder.model} (dim={deps.embedder.dimension})")
    print(f"[calibrate] database = {settings.database_url}")

    engine = create_engine(settings.database_url, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    try:
        with Session(engine, expire_on_commit=False) as session:
            kb_id = _build_corpus(session, deps)
            total = session.execute(text("SELECT count(*) FROM chunks")).scalar()
            actor = Actor(subject_id="calibrate", tenant_id="default")
            print(f"[calibrate] 语料入库完成：{total} 个分块")

            true_sim: list[float] = []
            true_dist: list[float] = []
            for case in HIT_CASES:
                question = str(case["question"])
                snippets = [str(snippet) for snippet in case["any_of"]]
                vector = deps.embedder.embed([question])[0]
                true_sim.append(_matching_similarity(session, question, snippets))
                true_dist.append(_matching_distance(session, question, vector, snippets))

            neg_sim: list[float] = []
            neg_dist: list[float] = []
            for question in NEGATIVE_QUESTIONS:
                vector = deps.embedder.embed([question])[0]
                worst_sim, best_dist = _corpus_extreme(session, question, vector)
                neg_sim.append(worst_sim)
                neg_dist.append(best_dist)

            print("\n== 关键词路（trigram 相似度） ==")
            print(f"  命中用例：min={min(true_sim):.3f} 中位={statistics.median(true_sim):.3f}")
            print(f"  负面问题：max={max(neg_sim):.3f}")
            kw_gap = min(true_sim) - max(neg_sim)
            if kw_gap > 0:
                floor = max(neg_sim) + kw_gap / 2
                print(f"  可分隔：推荐 KS_SIMILARITY_FLOOR ≈ {floor:.3f}"
                      f"（负面之上、真实之下各留 {kw_gap / 2:.3f}）")
            else:
                floor = round((min(true_sim) + max(neg_sim)) / 2, 3)
                print(f"  ⚠ 重叠 {abs(kw_gap):.3f}：关键词路不可分，"
                      f"取折中 {floor} 并依赖语义路兜底")

            print("\n== 语义路（余弦距离，越小越相关） ==")
            print(f"  命中用例：max={max(true_dist):.3f} 中位={statistics.median(true_dist):.3f}")
            print(f"  负面问题：min={min(neg_dist):.3f}")
            sem_gap = min(neg_dist) - max(true_dist)
            if sem_gap > 0:
                distance = max(true_dist) + sem_gap / 2
                print(f"  可分隔：推荐 KS_SEMANTIC_MAX_DISTANCE ≈ {distance:.3f}"
                      f"（真实之上、负面之下各留 {sem_gap / 2:.3f}）")
            else:
                distance = round((max(true_dist) + min(neg_dist)) / 2, 3)
                print(f"  ⚠ 重叠 {abs(sem_gap):.3f}：语义路不可分，取折中 {distance}，"
                      "建议扩充负面问题或更换向量模型")

            # 当前阈值下的排名参考
            hit1 = hit5 = hit10 = 0
            for case in HIT_CASES:
                question = str(case["question"])
                snippets = [str(snippet) for snippet in case["any_of"]]
                hits = retrieval.search(
                    session, actor, settings, deps.embedder,
                    query=question, kb_ids=[kb_id], mode="hybrid", top_k=10,
                )
                matched = [h for h in hits if any(sn in h.text for sn in snippets)]
                if matched:
                    rank = hits.index(matched[0]) + 1
                    hit1 += rank == 1
                    hit5 += rank <= 5
                    hit10 += rank <= 10
            count = len(HIT_CASES)
            print(f"\n== 当前配置排名参考 ==\n  hit@1={hit1}/{count}  "
                  f"hit@5={hit5}/{count}  hit@10={hit10}/{count}")
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
