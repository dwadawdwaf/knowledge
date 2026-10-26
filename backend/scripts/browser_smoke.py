"""使用独立本机浏览器实测页面；只接入自制资料和模型桩。"""

from __future__ import annotations

import io
import os
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from scripts.http_scenario import expect, upload_sample, wait_job
from scripts.local_runtime import BACKEND, LocalRuntime
from scripts.model_fixture import ModelFixture


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    candidates = [
        Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
        Path("C:/Program Files/Google/Chrome/Application/chrome.exe"),
    ]
    browser = next((path for path in candidates if path.is_file()), None)
    node = shutil.which("node")
    if browser is None or node is None:
        print(
            "需要已有 Node.js 22+ 与 Edge/Chrome；不会自动安装，也不会将缺失视为通过。", flush=True
        )
        return 1
    with (
        ModelFixture() as model,
        LocalRuntime(embedding_url=model.url, generator_url=model.url) as runtime,
        runtime.client() as client,
    ):
        for name in ("行政制度 · 自制样本", "信息安全 · 自制样本", "项目资料 · 自制样本"):
            kb = expect(client.post("/api/v1/knowledge-bases", json={"name": name}), 201)
            if name.startswith("行政"):
                result = upload_sample(client, kb["id"])
                wait_job(client, result["job_id"], progress=runtime.say)
        env = {key: value for key, value in os.environ.items() if not key.startswith("KS_")}
        env.update(
            KS_UI_BROWSER=str(browser),
            KS_UI_URL=runtime.base_url + "/workbench/",
            KS_UI_TOKEN=client.headers["Authorization"].removeprefix("Bearer "),
            PYTHONIOENCODING="utf-8",
        )
        return subprocess.call(
            [node, str(BACKEND.parent / "frontend" / "browser-smoke.mjs")],
            env=env,
            cwd=BACKEND.parent,
        )


if __name__ == "__main__":
    raise SystemExit(main())
