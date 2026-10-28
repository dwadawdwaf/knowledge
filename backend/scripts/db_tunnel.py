"""保持数据库隧道：断开后自动重连，由启动器拉起。

只做端口转发，不读业务数据；每次重连写一行日志，便于判断是不是网络在抖。
"""

from __future__ import annotations

import pathlib
import subprocess
import sys
import time
from urllib.parse import urlsplit

BACKEND = pathlib.Path(__file__).resolve().parents[1]
if str(BACKEND) not in sys.path:
    sys.path.insert(0, str(BACKEND))

from knowledge.config import Settings

RECONNECT_SECONDS = 3


def build_command(settings: Settings) -> list[str]:
    port = urlsplit(settings.database_url).port or 5432
    command = [
        "ssh",
        "-N",
        "-o",
        "BatchMode=yes",
        "-o",
        "ExitOnForwardFailure=yes",
        "-o",
        "ServerAliveInterval=10",
        "-o",
        "ServerAliveCountMax=3",
        "-o",
        "TCPKeepAlive=yes",
        "-L",
        f"{port}:127.0.0.1:5432",
        settings.tunnel_ssh or "",
    ]
    if settings.tunnel_key:
        command[1:1] = ["-i", settings.tunnel_key]
    return command


def main() -> int:
    settings = Settings()
    if not settings.tunnel_ssh:
        print("未配置 KS_TUNNEL_SSH，守护进程退出", file=sys.stderr, flush=True)
        return 2
    command = build_command(settings)
    while True:
        started = time.monotonic()
        print(f"[tunnel] 连接 {settings.tunnel_ssh}", flush=True)
        code = subprocess.call(command)
        lived = time.monotonic() - started
        print(
            f"[tunnel] 断开 code={code} 存活 {lived:.0f}s，{RECONNECT_SECONDS}s 后重连",
            flush=True,
        )
        time.sleep(RECONNECT_SECONDS)


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        raise SystemExit(0) from None
