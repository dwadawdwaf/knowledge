"""本地与 CI 共用检查入口；不安装依赖，不读取真实模型配置。"""

from __future__ import annotations

import os
import subprocess
import sys
import time
from importlib.metadata import PackageNotFoundError, version
from pathlib import Path

BACKEND = Path(__file__).resolve().parents[1]
SCRIPTS = (
    "local_runtime",
    "http_scenario",
    "model_fixture",
    "demo_local",
    "test_isolated",
    "real_model",
    "ask_console",
    "ask_local",
    "check",
    "test_policy",
    "workbench",
    "browser_smoke",
    "backfill_ragflow_terms",
    "prepare_ragflow_resources",
    "sync_native_index",
)


def check_environment() -> bool:
    expected_python = (BACKEND.parent / ".python-version").read_text(encoding="utf-8").strip()
    actual_python = ".".join(map(str, sys.version_info[:3]))
    if actual_python != expected_python:
        print(f"Python 版本不匹配：需要 {expected_python}，当前 {actual_python}", flush=True)
        return False
    valid = True
    for line in (BACKEND / "requirements-dev.lock").read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        name, expected = line.split("==")
        try:
            actual = version(name)
        except PackageNotFoundError:
            actual = "未安装"
        if actual != expected:
            print(f"依赖不匹配：{name} 需要 {expected}，当前 {actual}", flush=True)
            valid = False
    return valid


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    print("[预检] 检查 Python 与依赖锁文件，不自动安装或联网。", flush=True)
    if not check_environment():
        return 1
    started = time.monotonic()
    steps = [
        ("lint", ["-m", "ruff", "check", "knowledge", "tests", "scripts"]),
        ("业务类型检查", ["-m", "mypy", "knowledge"]),
        (
            "运行脚本类型检查",
            ["-m", "mypy", "--explicit-package-bases", *(f"scripts/{name}.py" for name in SCRIPTS)],
        ),
        ("完整隔离回归（含迁移和进程级 HTTP 验收）", ["-u", "scripts/test_isolated.py"]),
    ]
    env = {key: value for key, value in os.environ.items() if not key.startswith("KS_")}
    # 唯一允许继承的测试配置是本地字体文件路径，不继承数据库或模型端点。
    if "KS_TEST_FONT_PATH" in os.environ:
        env["KS_TEST_FONT_PATH"] = os.environ["KS_TEST_FONT_PATH"]
    env.update(PYTHONUNBUFFERED="1", PYTHONIOENCODING="utf-8", PYTEST_ADDOPTS="")
    env.pop("PYTEST_PLUGINS", None)
    for number, (title, arguments) in enumerate(steps, 1):
        print(f"\n[{number}/{len(steps)}] {title}", flush=True)
        result = subprocess.call([sys.executable, *arguments], cwd=BACKEND, env=env)
        if result:
            print(f"检查失败：{title}，退出码 {result}；后续步骤未执行。", flush=True)
            return result
        print(f"通过：{title}（累计 {time.monotonic() - started:.1f} 秒）", flush=True)
    print("\n完整检查通过；未调用真实模型。", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
