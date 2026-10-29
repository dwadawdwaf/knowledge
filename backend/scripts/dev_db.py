"""本机数据库：确保内置 PostgreSQL 在跑，打印连接串。

启动器会自动调用同样的逻辑（`scripts/local_db.py`），这个脚本只用于手动排查。

用法：
    python scripts/dev_db.py              # 启动（或复用）本机库
    python scripts/dev_db.py --reset      # 停掉并删除数据目录后重建
    python scripts/dev_db.py --stop       # 停止本机库

数据目录默认：项目内 `backend/.data/pgdata`（已初始化时），否则用户目录下的 ks-postgres。
"""

from __future__ import annotations

import argparse
import pathlib
import shutil
import signal
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.local_db import DEFAULT_PORT, data_dir, ensure, stop_instance


def stop() -> None:
    """干净关闭本机库（pg_ctl fast），避免残留共享内存块让下一个实例起不来。"""
    if stop_instance(data_dir()):
        print("[db] 已停止", flush=True)
    else:
        print("[db] 没有运行中的本机库", flush=True)


def main() -> int:
    parser = argparse.ArgumentParser(description="本机 PostgreSQL（内置二进制）")
    parser.add_argument("--reset", action="store_true", help="删除数据目录后重建")
    parser.add_argument("--stop", action="store_true", help="停止本机库")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT)
    args = parser.parse_args()

    if args.stop:
        stop()
        return 0

    if args.reset:
        stop()
        directory = data_dir()
        shutil.rmtree(directory, ignore_errors=True)
        print(f"[db] 已删除数据目录：{directory}", flush=True)

    url = ensure(print, port=args.port)
    print(f"[db] 连接串 {url}", flush=True)
    print("[db] 数据库在后台运行，关掉终端不会停；停止用 python scripts/dev_db.py --stop", flush=True)
    return 0


if __name__ == "__main__":
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    raise SystemExit(main())
