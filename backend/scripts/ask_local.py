"""启动真实模型终端问答；只加载模型配置，数据库与资料卷仍为隔离临时环境。"""

from __future__ import annotations

import argparse
import io
import os
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

import httpx

from scripts.ask_console import ask_once, run_console
from scripts.deps import DependencyError, ensure_dependencies
from scripts.http_scenario import expect, upload_sample, wait_job
from scripts.local_runtime import LocalRuntime
from scripts.real_model import load_real_settings
from scripts.workbench import open_workbench


def main() -> int:
    for stream in (sys.stdin, sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="本机 API 端口；0 自动选择")
    parser.add_argument("--check", action="store_true", help="仅检查配置，不调用模型或启动服务")
    parser.add_argument("--pull-missing", action="store_true", help="缺少向量模型时自动拉取")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器工作台")
    parser.add_argument("--question", help="执行一次问题后退出，便于复验；不会保存问答正文")
    args = parser.parse_args()
    try:
        settings = load_real_settings()
        print(f"向量模型：{settings.embedding_model}（{settings.embedding_dim} 维）", flush=True)
        print(f"生成模型：{settings.generator_model}", flush=True)
        print("模型配置检查通过；未输出 API 密钥。", flush=True)
        if args.check:
            return 0
        if not os.environ.get("KS_DEPS_READY"):
            ensure_dependencies(
                settings,
                pull_missing=args.pull_missing,
                progress=lambda message: print(message, flush=True),
            )
        with (
            LocalRuntime(port=args.port, model_settings=settings) as runtime,
            runtime.client() as client,
        ):
            runtime.say("[准备] 建立真实模型体验库，仅导入自制会议室资料")
            kb = expect(
                client.post("/api/v1/knowledge-bases", json={"name": "真实模型体验库（自制资料）"}),
                201,
            )
            uploaded = upload_sample(client, kb["id"])
            wait_job(client, uploaded["job_id"], progress=runtime.say, timeout=180)
            runtime.say(f"[就绪] {runtime.base_url}/docs；知识库 {kb['id']}")
            print(
                "自制资料涵盖：会议室使用后事项、预约提前时间。其他问题可能没有证据。", flush=True
            )
            if args.question:
                return 0 if ask_once(client, kb["id"], args.question) else 1
            open_workbench(runtime, browser=not args.no_browser)
            run_console(client, kb["id"])
        return 0
    except KeyboardInterrupt:
        print("\n已停止问答，正在关闭本次临时服务。", flush=True)
        return 0
    except ValueError as exc:
        print(f"配置检查失败：{exc}", flush=True)
        return 1
    except DependencyError as exc:
        print(f"依赖未就绪：{exc}", flush=True)
        return 1
    except AssertionError as exc:
        reason = str(exc).strip()
        if reason:
            print(f"启动或资料处理失败：{reason}", flush=True)
        print(
            "请检查模型端点、名称、1024 维配置、余额及任务错误码；不会回退到模型桩。",
            flush=True,
        )
        return 1
    except (RuntimeError, httpx.TransportError):
        print("本地服务未就绪，请查看上方进度和日志路径；不输出内部异常详情。", flush=True)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
