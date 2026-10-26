"""创建独立临时 PostgreSQL 执行离线回归，不使用现有业务数据库。"""

from __future__ import annotations

import io
import os
import pathlib
import subprocess
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from scripts.local_runtime import isolated_database


def main() -> int:
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(encoding="utf-8")
    root = pathlib.Path(__file__).resolve().parents[2]
    started = time.monotonic()

    def progress(message: str) -> None:
        print(f"[{time.monotonic() - started:6.1f}s] {message}", flush=True)

    progress("[tests] Preparing isolated databases; no external model calls")
    with isolated_database(progress) as dsn:
        import psycopg

        with psycopg.connect(
            dsn.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
        ) as connection:
            connection.execute("CREATE DATABASE migration_test")
            connection.execute("CREATE DATABASE e2e_test")
        env = dict(
            {key: value for key, value in os.environ.items() if not key.startswith("KS_")},
            KS_DATABASE_URL=dsn,
            KS_TEST_DATABASE_URL=dsn,
            KS_TEST_MIGRATION_DATABASE_URL=dsn.rsplit("/", 1)[0] + "/migration_test",
            KS_E2E_DATABASE_URL=dsn.rsplit("/", 1)[0] + "/e2e_test",
            KS_RUN_E2E="1",
            PYTHONUNBUFFERED="1",
            PYTHONIOENCODING="utf-8",
            KS_EMBEDDING_BASE_URL="",
            KS_GENERATOR_BASE_URL="",
            KS_EMBEDDING_API_KEY="",
            KS_GENERATOR_API_KEY="",
        )
        if "KS_TEST_FONT_PATH" in os.environ:
            env["KS_TEST_FONT_PATH"] = os.environ["KS_TEST_FONT_PATH"]
        env.pop("KS_TEST_REAL_EMBEDDER", None)
        env["PYTHONPATH"] = str(root / "backend")
        env["PYTEST_ADDOPTS"] = ""
        env.pop("PYTEST_PLUGINS", None)
        progress("[tests] Databases ready; running pytest with live case/step output")
        result = subprocess.call(
            [
                sys.executable,
                "-u",
                "-m",
                "pytest",
                "-p",
                "scripts.test_policy",
                "backend/tests",
                "-v",
                "--capture=sys",
                *sys.argv[1:],
            ],
            cwd=root,
            env=env,
        )
        progress(f"[tests] Finished: exit_code={result}")
        return result


if __name__ == "__main__":
    raise SystemExit(main())
