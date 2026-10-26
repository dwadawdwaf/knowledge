"""本地开发数据库：启动内置 PostgreSQL（含 pgvector），无需安装 PostgreSQL。

用法：
    python scripts/dev_db.py                 # 前台运行，Ctrl+C 停止
    python scripts/dev_db.py --reset         # 删除数据目录后重建
    python scripts/dev_db.py --data-dir D:/pgdata

启动后写入 `.data/db_url`（开发库）与 `.data/db_test_url`（测试库）：
    export KS_DATABASE_URL=$(cat .data/db_url)
"""

from __future__ import annotations

import argparse
import os
import pathlib
import shutil
import tempfile
import time
from types import ModuleType

DATA_URL = pathlib.Path(".data/db_url")
TEST_URL = pathlib.Path(".data/db_test_url")


def _prepare_environment(data_dir: pathlib.Path) -> None:
    """中文 Windows 下 PostgreSQL 输出为 GBK，包内按 UTF-8 解码会失败。"""
    os.environ.setdefault("LC_ALL", "C")
    os.environ.setdefault("LANG", "C")

    if not str(data_dir).isascii():
        raise SystemExit(f"数据目录必须为纯英文路径（postmaster.pid 按本地编码写入）：{data_dir}")

    import embedded_postgres._commands as commands

    class _TempFile(ModuleType):
        @staticmethod
        def TemporaryFile(*args, **kwargs):
            kwargs.setdefault("encoding", "utf-8")
            kwargs.setdefault("errors", "replace")
            return tempfile.TemporaryFile(*args, **kwargs)

    commands.tempfile = _TempFile("embedded_postgres_tempfile")  # 仅替换该模块内的引用


def main() -> int:
    default_dir = pathlib.Path(tempfile.gettempdir()) / "ks_pgdata"
    parser = argparse.ArgumentParser(description="启动内置 PostgreSQL（开发用）")
    parser.add_argument("--data-dir", default=str(default_dir), help="数据目录，必须为纯英文路径")
    parser.add_argument("--reset", action="store_true", help="删除数据目录后重建")
    args = parser.parse_args()

    data_dir = pathlib.Path(args.data_dir).resolve()
    _prepare_environment(data_dir)

    if args.reset and data_dir.exists():
        print(f"[db] removing {data_dir}", flush=True)
        shutil.rmtree(data_dir, ignore_errors=True)

    import embedded_postgres as ep

    with ep.get_server(str(data_dir)) as pg:
        pg.psql("CREATE EXTENSION IF NOT EXISTS vector")
        pg.psql("CREATE EXTENSION IF NOT EXISTS pg_trgm")

        dsn = pg.get_uri().replace("postgresql://", "postgresql+psycopg://")
        test_dsn = dsn.rsplit("/", 1)[0] + "/ks_test"
        try:
            pg.psql("CREATE DATABASE ks_test")
        except Exception:  # noqa: BLE001, S110 - 数据库已存在即忽略
            pass

        DATA_URL.parent.mkdir(parents=True, exist_ok=True)
        DATA_URL.write_text(dsn, encoding="utf-8")
        TEST_URL.write_text(test_dsn, encoding="utf-8")

        version = pg.psql("SELECT version()").splitlines()[2].strip()
        print(f"[db] {version}", flush=True)
        print(f"[db] dev  {dsn}", flush=True)
        print(f"[db] test {test_dsn}", flush=True)
        print("[db] Ctrl+C 停止", flush=True)

        try:
            while True:
                time.sleep(3600)
        except KeyboardInterrupt:
            print("[db] stopping", flush=True)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
