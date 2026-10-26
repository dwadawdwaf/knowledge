"""终端问答客户端：只调用 REST API，展示服务端校验后的引用，不保存用户问题。"""

from __future__ import annotations

import sys
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import TextIO

import httpx


def _safe_text(value: object) -> str:
    return "".join(
        char
        for char in str(value)
        if char in "\n\t" or ord(char) >= 32 and not 127 <= ord(char) <= 159
    )


@contextmanager
def _waiting(output: TextIO) -> Iterator[None]:
    stop = threading.Event()
    started = time.monotonic()

    def report() -> None:
        while not stop.wait(3):
            print(
                f"  正在检索与生成，已等待 {time.monotonic() - started:.0f} 秒...",
                file=output,
                flush=True,
            )

    thread = threading.Thread(target=report, daemon=True)
    thread.start()
    try:
        yield
    finally:
        stop.set()
        thread.join(timeout=1)


def ask_once(
    client: httpx.Client, kb_id: str, question: str, *, output: TextIO | None = None
) -> bool:
    stream = output or sys.stdout
    print("正在检索知识库并请求回答...", file=stream, flush=True)
    try:
        with _waiting(stream):
            response = client.post(
                "/api/v1/qa", json={"question": question, "knowledge_base_ids": [kb_id]}
            )
        body = response.json()
    except httpx.TimeoutException:
        print("请求等待超时，未获得回答；不会回退到模型桩。", file=stream, flush=True)
        return False
    except (httpx.TransportError, ValueError):
        print("无法取得服务响应，请检查 API 状态和运行日志。", file=stream, flush=True)
        return False
    if not isinstance(body, dict):
        print("服务响应格式不正确，未取得可展示的回答。", file=stream, flush=True)
        return False
    if response.status_code != 200:
        error = body.get("error", {})
        error = error if isinstance(error, dict) else {}
        code = error.get("code", "INTERNAL_ERROR")
        allowed = {
            "MODEL_UNAVAILABLE",
            "MODEL_TIMEOUT",
            "CITATION_VALIDATION_FAILED",
            "SOURCE_CHANGED",
            "VALIDATION_ERROR",
            "RESOURCE_NOT_FOUND",
            "UNAUTHENTICATED",
        }
        code = code if isinstance(code, str) and code in allowed else "INTERNAL_ERROR"
        print(
            f"请求失败：HTTP {response.status_code} / {code}；未返回答案。", file=stream, flush=True
        )
        return False
    data = body.get("data")
    if (
        not isinstance(data, dict)
        or not isinstance(data.get("answer"), str)
        or not isinstance(data.get("answer_status"), str)
    ):
        print("服务响应缺少答案或状态，未取得可展示的回答。", file=stream, flush=True)
        return False
    print(
        f"\n状态：{_safe_text(data['answer_status'])}\n回答：\n{_safe_text(data['answer'])}",
        file=stream,
        flush=True,
    )
    for citation in data.get("citations", []):
        locator = citation.get("locator", {})
        position = []
        if locator.get("page") is not None:
            position.append(f"第 {locator['page']} 页")
        if locator.get("paragraph") is not None:
            position.append(f"段落索引 {locator['paragraph']}")
        if locator.get("char_start") is not None:
            position.append(f"字符 [{locator['char_start']}, {locator.get('char_end')})")
        print(f"\n引用 [{citation['index']}] {_safe_text(citation['title'])}", file=stream)
        print(
            "  定位：" + _safe_text("；".join(position) or locator.get("type", "未知")), file=stream
        )
        print("  原文：" + _safe_text(citation["excerpt"]), file=stream, flush=True)
    if not data.get("citations"):
        print("引用：无。", file=stream, flush=True)
    for warning in data.get("warnings", []):
        print("提示：" + _safe_text(warning), file=stream, flush=True)
    return True


def run_console(
    client: httpx.Client,
    kb_id: str,
    *,
    read: Callable[[str], str] = input,
    output: TextIO | None = None,
) -> None:
    stream = output or sys.stdout
    print("\n可以开始提问。输入 /quit 退出；Ctrl+C 停止服务。", file=stream, flush=True)
    print("终端仅展示本轮回答，不将问题、答案或令牌写入报告和服务日志。", file=stream, flush=True)
    print(
        "浏览器工作台：在本次 API 地址后加 /workbench/。输入 /token 显式显示开发令牌。",
        file=stream,
        flush=True,
    )
    while True:
        try:
            question = read("\n请输入问题 > ").strip()
        except EOFError:
            return
        if question.lower() in ("/quit", "/exit"):
            return
        if question.lower() == "/token":
            print("仅供本次本机开发连接；不要截图、提交或转发：", file=stream, flush=True)
            print(
                client.headers.get("Authorization", "").removeprefix("Bearer "),
                file=stream,
                flush=True,
            )
            continue
        if question:
            ask_once(client, kb_id, question, output=stream)
