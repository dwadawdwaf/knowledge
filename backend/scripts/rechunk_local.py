"""在工作台持久库中用本机解析器和向量模型重切文档，不调用生成模型。"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

import psutil

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowledge.domain.entities import JobState
from knowledge.domain.errors import DomainError
from knowledge.repositories import jobs, reindex
from knowledge.repositories.session import new_session
from knowledge.security import issue_token, verify_token
from knowledge.services.document import rechunk_existing
from knowledge.worker.loop import build_deps, process_next
from scripts.local_db import can_connect, database_url
from scripts.local_runtime import WORKBENCH_OBJECTS, WORKBENCH_OWNER
from scripts.real_model import external_database_url, load_real_settings


def workbench_running() -> bool:
    for process in psutil.process_iter(["cmdline"], ad_value=None):
        args = process.info["cmdline"] or []
        if "knowledge.entrypoints.worker" in args or "knowledge.entrypoints.api:app" in args:
            return True
    return False


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="实际重切；省略时只统计文档数")
    parser.add_argument("--include-current", action="store_true", help="连已使用现行策略的文档一起重切")
    args = parser.parse_args()
    settings = load_real_settings()
    if external_database_url(settings) is not None or os.environ.get("KS_WORKBENCH_OBJECTS"):
        parser.exit(1, "仅支持默认的本机工作台持久库和原件卷。\n")
    for endpoint in (settings.embedding_base_url, settings.mineru_url):
        if not endpoint or urlsplit(endpoint).hostname not in {"127.0.0.1", "::1", "localhost"}:
            parser.exit(1, "重切只允许本机向量服务和解析服务。\n")
    if args.apply and workbench_running():
        parser.exit(1, "请先退出工作台，避免旧 Worker 抢占迁移任务。\n")
    dsn = database_url()
    if not can_connect(dsn) or not WORKBENCH_OBJECTS.is_dir():
        parser.exit(1, "本机工作台库或持久原件卷不可用；未创建新库。\n")
    os.environ["KS_DATABASE_URL"] = dsn
    settings = settings.model_copy(update={
        "database_url": dsn, "storage_root": str(WORKBENCH_OBJECTS),
        "generator_base_url": None, "generator_api_key": None, "opensearch_url": None,
    })
    # 仅限本机开发工作台使用同一可信主体，不接受命令行自报的 owner。
    actor = verify_token(issue_token(WORKBENCH_OWNER, settings=settings), settings=settings)
    with new_session() as session:
        ids = reindex.owned_document_ids(session, actor)
    print(f"工作台可访问文档：{len(ids)}", flush=True)
    if not args.apply:
        return 0
    deps = build_deps(settings)
    outcomes: Counter[str] = Counter()
    for index, doc_id in enumerate(ids, 1):
        if workbench_running():
            print("检测到工作台重新启动，停止提交剩余任务。", flush=True)
            return 1
        with new_session() as session:
            try:
                ack = rechunk_existing(
                    session, actor, settings, document_id=doc_id,
                    embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
                    include_current=args.include_current,
                )
            except DomainError as exc:
                outcomes[exc.code] += 1
                print(f"[{index}/{len(ids)}] {exc.code}", flush=True)
                continue
            if ack is None:
                outcomes["current"] += 1
                print(f"[{index}/{len(ids)}] 已是现行策略", flush=True)
                continue
            print(f"[{index}/{len(ids)}] 重切任务 {ack.job_id}", flush=True)
            deadline = time.monotonic() + settings.mineru_timeout_seconds * settings.max_attempts + 180
            while time.monotonic() < deadline:
                process_next(session, deps, target_job_id=ack.job_id)
                session.expire_all()
                job = jobs.get_owned(session, actor, ack.job_id)
                state, error = job.state, job.last_error
                session.rollback()
                if state not in {JobState.RUNNING, JobState.QUEUED}:
                    code = (error or {}).get("code", state.value)
                    outcomes[code] += 1
                    print(f"[{index}/{len(ids)}] {code}", flush=True)
                    break
                time.sleep(0.5)
            else:
                print("任务未在维护时间内结束；停止提交剩余任务。", flush=True)
                return 1
    print(f"完成统计：{dict(outcomes)}", flush=True)
    return int(any(key not in {"succeeded", "current"} for key in outcomes))


if __name__ == "__main__":
    raise SystemExit(main())
