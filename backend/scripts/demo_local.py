"""可观察的本地 HTTP 演示；--serve 在验收后保留 Swagger 供手动体验。"""

from __future__ import annotations

import argparse
import io
import pathlib
import sys
from datetime import UTC, datetime

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.ask_console import run_console
from scripts.http_scenario import expect, run_lifecycle, upload_sample, wait_job
from scripts.local_runtime import BACKEND, LocalRuntime
from scripts.workbench import open_workbench


def write_report(result: dict) -> pathlib.Path:
    target = BACKEND / ".data" / "demo" / "latest.md"
    target.parent.mkdir(parents=True, exist_ok=True)
    citation = result["answer"]["citations"][0]
    text = (
        "# 本地 HTTP 演示：实际运行结果\n\n"
        f"生成时间：{datetime.now(UTC).isoformat()}\n\n"
        "资料为仓库自制样本；向量为 HashEmbedder，生成为 StubGenerator。"
        "以下内容取自本次实际 HTTP 响应，不是真实模型效果评测，也不是预填答案。\n\n"
        f"## 问题\n\n{result['question']}\n\n"
        f"## 版本一回答\n\n{result['answer']['answer']}\n\n"
        f"## 实际引用\n\n- 文档：{citation['title']}\n"
        f"- 版本：`{citation['version_id']}`\n- 构建：`{citation['build_id']}`\n"
        f"- 定位：`{citation['locator']}`\n\n> {citation['excerpt']}\n\n"
        f"## 更新到版本二后的回答\n\n{result['updated_answer']['answer']}\n\n"
        f"## 无关问题\n\n状态：`{result['missing']['answer_status']}`\n\n"
        "## 已验证\n\n"
        "- 独立 API、Worker 与真实临时 PostgreSQL，通过 HTTP 完成 10 步验收。\n"
        "- 版本二排队时，版本一仍可检索；发布后只检索当前构建。\n"
        "- 他人读取文档、任务、引用及指定库检索，均返回 404。\n"
        "- 暂停 Worker 后删除，引用立即 404、检索 0 命中；恢复 Worker 后清理成功。\n"
        "- 此报告不覆盖真实模型效果，也不单独证明进程崩溃恢复；后者由 E2E 测试验证。\n"
    )
    target.write_text(text, encoding="utf-8")
    return target


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--serve", action="store_true", help="Keep demo services running for manual Swagger use"
    )
    parser.add_argument(
        "--port", type=int, default=0, help="Local API port; default selects a free port"
    )
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器工作台")
    args = parser.parse_args()
    try:
        with LocalRuntime(port=args.port) as runtime:
            result = run_lifecycle(runtime)
            report = write_report(result)
            runtime.say(f"[report] Actual answers and citations saved: {report}")
            if args.serve:
                with runtime.client() as client:
                    kb = expect(
                        client.post("/api/v1/knowledge-bases", json={"name": "手动体验库"}), 201
                    )
                    uploaded = upload_sample(client, kb["id"])
                    wait_job(client, uploaded["job_id"], progress=runtime.say)
                runtime.say(f"[manual] Swagger: {runtime.base_url}/docs")
                runtime.say(f"[manual] subject={runtime.owner}; knowledge_base_id={kb['id']}")
                runtime.say(
                    "[manual] Suggested question and actual answers: see .data/demo/latest.md"
                )
                runtime.say(
                    "[manual] Press Ctrl+C to stop. Dev authentication instructions: docs/runbook.md"
                )
                with runtime.client() as client:
                    open_workbench(runtime, browser=not args.no_browser)
                    run_console(client, kb["id"])
    except KeyboardInterrupt:
        print("[demo] Stopped; owned API/Worker/database shut down", flush=True)
        return 0
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
