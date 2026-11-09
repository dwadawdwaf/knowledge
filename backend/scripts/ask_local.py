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
from scripts.deps import DependencyError, ensure_database, ensure_dependencies
from scripts.http_scenario import expect, upload_sample, wait_job
from scripts.local_runtime import WORKBENCH_OBJECTS, LocalRuntime
from scripts.real_model import load_real_settings
from scripts.workbench import open_workbench


def ensure_experience_kb(client: httpx.Client) -> tuple[dict, bool]:
    """复用同名体验库，返回 (知识库, 是否需要导入内置资料)。

    持久库里每次启动都新建同名库，会让「知识库」列表堆满空副本；
    已有同名库且其中已有文档时直接复用，不再写入。
    """
    name = "真实模型体验库（自制资料）"
    items = expect(client.get("/api/v1/knowledge-bases"), 200).get("items", [])
    existing = next(
        (kb for kb in items if kb.get("name") == name and kb.get("status") == "active"), None
    )
    if existing is None:
        return expect(client.post("/api/v1/knowledge-bases", json={"name": name}), 201), True
    documents = expect(client.get(f"/api/v1/knowledge-bases/{existing['id']}/documents"), 200)
    return existing, not documents.get("items")


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
        settings = load_real_settings(workbench=True)
        print(f"向量模型：{settings.embedding_model}（{settings.embedding_dim} 维）", flush=True)
        print(f"生成模型：{settings.generator_model}", flush=True)
        print(f"检索方案：{settings.retrieval_profile}", flush=True)
        print("模型配置检查通过；未输出 API 密钥。", flush=True)
        if args.check:
            return 0
        if not os.environ.get("KS_DEPS_READY"):
            ensure_dependencies(
                settings,
                pull_missing=args.pull_missing,
                progress=lambda message: print(message, flush=True),
            )
        # 数据库默认用本机内置库（数据落在用户目录），配置了 KS_DATABASE_URL 才走远端。
        database_url = ensure_database(
            settings, progress=lambda message: print(message, flush=True)
        )
        with (
            LocalRuntime(
                port=args.port,
                model_settings=settings,
                database_url=database_url,
                # 库是持久的，卷也必须持久：临时卷会让旧文档的原文在下次启动时全部 404。
                storage_root=str(WORKBENCH_OBJECTS),
            ) as runtime,
            runtime.client() as client,
        ):
            runtime.say("[准备] 复用或建立真实模型体验库，内置资料只导一次")
            kb, seeded = ensure_experience_kb(client)
            if seeded:
                uploaded = upload_sample(client, kb["id"])
                wait_job(client, uploaded["job_id"], progress=runtime.say, timeout=180)
            else:
                runtime.say("[准备] 体验库已有资料，本次不重复导入")
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
