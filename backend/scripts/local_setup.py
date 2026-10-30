"""把数据搬到本机：起本机库 →（可选）从远端导入 → 自检。

只在迁移/排障时手动跑一次；日常启动走 `start.cmd`，不需要它。

用法：
    python scripts/local_setup.py                      # 只起本机库并自检
    python scripts/local_setup.py --remote-url <URL>    # 起库 + 从远端库导入数据
    python scripts/local_setup.py --remote-url <URL> --tunnel root@1.2.3.4 --tunnel-key C:/path/id_rsa

远端库地址里含口令，所以不写在脚本里、也不进提交，只从命令行或环境变量传入。
"""

from __future__ import annotations

import argparse
import os
import pathlib
import socket
import subprocess
import sys
import tempfile
import time

BACKEND = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from scripts import local_db

# 只用 CREATE_NO_WINDOW：带 DETACHED_PROCESS 时 PostgreSQL 的每个 backend 都会自己开控制台，
# 导数据时每连一次闪一个窗。隐藏控制台会被子进程继承，才是安静的做法。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
TABLES = (
    "knowledge_bases",
    "documents",
    "document_versions",
    "index_builds",
    "chunks",
    "jobs",
    "conversations",
    "messages",
)


def say(message: str) -> None:
    print(message, flush=True)


def port_open(host: str, port: int, timeout: float = 1.5) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def binary(name: str) -> pathlib.Path:
    path = local_db.bin_dir() / f"{name}.exe"
    return path if path.is_file() else local_db.bin_dir() / name


def start_tunnel(target: str, key: str | None, port: int) -> subprocess.Popen | None:
    """临时隧道：导入完就关，不像守护进程那样常驻。"""
    command = [
        "ssh", "-N",
        "-o", "BatchMode=yes",
        "-o", "ExitOnForwardFailure=yes",
        "-o", "ServerAliveInterval=10",
        "-L", f"{port}:127.0.0.1:5432",
    ]
    if key:
        command += ["-i", key]
    command.append(target)
    say(f"[迁移] 建立临时隧道 {target} -> 127.0.0.1:{port}")
    process = subprocess.Popen(
        command,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=_NO_WINDOW,
    )
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        if port_open("127.0.0.1", port):
            return process
        time.sleep(0.4)
    process.terminate()
    say("[迁移] 隧道没起来，跳过导入")
    return None


def import_remote(remote_url: str, local_url: str, tunnel: str | None, key: str | None) -> bool:
    """整库搬运：pg_dump(远端) -> psql(本机)。失败不致命，启动器仍可用空库。"""
    from urllib.parse import urlsplit

    parsed = urlsplit(remote_url)
    port = parsed.port or 5432
    tunnel_process = None
    if not port_open(parsed.hostname or "", port):
        if not tunnel:
            say(f"[迁移] 远端 {parsed.hostname}:{port} 不通，且未提供 --tunnel；跳过导入")
            return False
        tunnel_process = start_tunnel(tunnel, key, port)
        if tunnel_process is None:
            return False

    dump_path = pathlib.Path(tempfile.gettempdir()) / "ks_remote_dump.sql"
    try:
        say("[迁移] 导出远端数据…")
        dump = subprocess.run(
            [
                str(binary("pg_dump")),
                "--clean", "--if-exists", "--no-owner", "--no-privileges",
                "--no-tablespaces", "-f", str(dump_path), remote_url,
            ],
            check=False, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=_NO_WINDOW,
        )
        if dump.returncode != 0:
            say(f"[迁移] 导出失败：{(dump.stderr or '').strip().splitlines()[-1:] or '未知原因'}")
            return False

        say(f"[迁移] 导入本机库（{dump_path.stat().st_size // 1024} KB）…")
        restore = subprocess.run(
            [
                str(binary("psql")), "-q", "-v", "ON_ERROR_STOP=1",
                "-d", local_url.replace("postgresql+psycopg://", "postgresql://"),
                "-f", str(dump_path),
            ],
            check=False, capture_output=True, text=True, encoding="utf-8",
            errors="replace", creationflags=_NO_WINDOW,
        )
        if restore.returncode != 0:
            tail = (restore.stderr or "").strip().splitlines()[-3:]
            say("[迁移] 导入有报错：" + " | ".join(tail))
            return False
        say("[迁移] 导入完成")
        return True
    finally:
        dump_path.unlink(missing_ok=True)
        if tunnel_process is not None:
            tunnel_process.terminate()
            say("[迁移] 临时隧道已关闭")


def counts(url: str) -> dict[str, int]:
    import psycopg

    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    result: dict[str, int] = {}
    with psycopg.connect(dsn, connect_timeout=8) as connection, connection.cursor() as cursor:
        for table in TABLES:
            try:
                cursor.execute(f"select count(*) from {table}")
                result[table] = cursor.fetchone()[0]
            except Exception:  # noqa: BLE001 - 表还没建时按 0 处理
                result[table] = 0
    return result


def main() -> int:
    parser = argparse.ArgumentParser(description="本机库初始化与数据搬运")
    parser.add_argument("--remote-url", default=os.environ.get("KS_REMOTE_DATABASE_URL"))
    parser.add_argument("--tunnel", default=os.environ.get("KS_REMOTE_TUNNEL"))
    parser.add_argument("--tunnel-key", default=os.environ.get("KS_REMOTE_TUNNEL_KEY"))
    parser.add_argument("--skip-import", action="store_true", help="只起库，不导入远端数据")
    args = parser.parse_args()

    say("[1/3] 启动本机数据库")
    url = local_db.ensure(say, wait_seconds=180)

    say("[2/3] 数据")
    imported = False
    if args.skip_import or not args.remote_url:
        say("[迁移] 未提供远端库地址，跳过导入（本机库保持为空，首次启动会自动建表）")
    else:
        imported = import_remote(args.remote_url, url, args.tunnel, args.tunnel_key)

    say("[3/3] 自检")
    result = counts(url)
    for table, value in result.items():
        say(f"    {table:<20} {value}")
    say(f"[完成] 连接串 {url}")
    if not imported and not any(result.values()):
        say("    提示：库还是空的；把 --remote-url 传进来可以导入原有数据。")
    say("    下一步：双击 start.cmd（或 start.ps1）启动工作台。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
