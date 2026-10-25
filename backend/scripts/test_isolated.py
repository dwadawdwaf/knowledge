"""Run offline tests against a new disposable PostgreSQL, never an existing DB."""

from __future__ import annotations

import os
import pathlib
import shutil
import subprocess
import sys
import tempfile

from dev_db import _prepare_environment


def main() -> int:
    root = pathlib.Path(__file__).resolve().parents[2]
    data_dir = pathlib.Path(tempfile.mkdtemp(prefix="ks_audit_test_"))
    _prepare_environment(data_dir)
    import embedded_postgres as ep
    import embedded_postgres._commands as commands

    # Windows PostgreSQL cannot initialize from a non-ASCII installation path.
    if not str(commands.POSTGRES_BIN_PATH).isascii():
        install = pathlib.Path(tempfile.mkdtemp(prefix="ks_audit_pg_")) / "pginstall"
        shutil.copytree(commands.POSTGRES_BIN_PATH.parent, install)
        commands.POSTGRES_BIN_PATH = install / "bin"

    with ep.get_server(str(data_dir)) as pg:
        import psycopg

        with psycopg.connect(pg.get_uri(), autocommit=True) as connection:
            connection.execute("CREATE DATABASE migration_test")
        dsn = pg.get_uri().replace("postgresql://", "postgresql+psycopg://")
        env = dict(
            os.environ,
            KS_DATABASE_URL=dsn,
            KS_TEST_DATABASE_URL=dsn,
            KS_TEST_MIGRATION_DATABASE_URL=dsn.rsplit("/", 1)[0] + "/migration_test",
            KS_EMBEDDING_BASE_URL="",
            KS_GENERATOR_BASE_URL="",
            KS_EMBEDDING_API_KEY="",
            KS_GENERATOR_API_KEY="",
        )
        env.pop("KS_TEST_REAL_EMBEDDER", None)
        return subprocess.call(
            [sys.executable, "-m", "pytest", "backend/tests", "-q", *sys.argv[1:]],
            cwd=root,
            env=env,
        )


if __name__ == "__main__":
    raise SystemExit(main())
