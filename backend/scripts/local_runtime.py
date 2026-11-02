"""演示与 HTTP 端到端测试的临时运行环境，不重置现有数据库。"""

from __future__ import annotations

import os
import pathlib
import secrets
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import uuid
from collections.abc import Callable, Iterator
from contextlib import ExitStack, contextmanager
from typing import BinaryIO, Self

import httpx

from knowledge.config import Settings
from knowledge.security import issue_token
from scripts.local_db import prepare_embedded_env
from scripts.real_model import real_model_environment

BACKEND = pathlib.Path(__file__).resolve().parents[1]

# 持久库配套的对象卷：同一套库重复启动必须落在同一个目录，否则原文全部 404。
WORKBENCH_OBJECTS = pathlib.Path(os.environ.get("KS_WORKBENCH_OBJECTS") or BACKEND / ".data" / "objects")


@contextmanager
def isolated_database(progress: Callable[[str], None]) -> Iterator[str]:
    progress("[database] Initializing NEW temporary PostgreSQL (existing data untouched)")
    with tempfile.TemporaryDirectory(
        prefix="ks_runtime_pg_", ignore_cleanup_errors=True
    ) as temporary:
        directory = pathlib.Path(temporary).resolve()
        if not directory.is_relative_to(pathlib.Path(tempfile.gettempdir()).resolve()):
            raise RuntimeError("Unexpected temporary database directory")
        prepare_embedded_env(directory)
        import embedded_postgres as ep
        import embedded_postgres._commands as commands
        import embedded_postgres.postgres_server as server_module

        original = commands.POSTGRES_BIN_PATH
        try:
            if not str(original).isascii():
                progress("[database] Preparing ASCII installation path for Windows")
                installation = directory / "pginstall"
                shutil.copytree(original.parent, installation)
                commands.POSTGRES_BIN_PATH = installation / "bin"
                server_module.POSTGRES_BIN_PATH = installation / "bin"
            with ep.get_server(str(directory / "data")) as pg:
                progress("[database] PostgreSQL ready")
                yield pg.get_uri().replace("postgresql://", "postgresql+psycopg://")
            progress("[database] PostgreSQL stopped")
        finally:
            commands.POSTGRES_BIN_PATH = original
            server_module.POSTGRES_BIN_PATH = original


class LocalRuntime:
    def __init__(
        self,
        *,
        database_url: str | None = None,
        embedding_url: str = "",
        generator_url: str = "",
        port: int = 0,
        model_settings: Settings | None = None,
        lease_seconds: int | None = None,
        heartbeat_seconds: int | None = None,
        storage_root: str | None = None,
    ) -> None:
        self.database_url = database_url
        self.embedding_url = embedding_url
        self.generator_url = generator_url
        self.port = port
        self.model_settings = model_settings
        # 外部库的资料卷必须跟着库走：数据库持久而对象留在临时目录，
        # 下一次运行就只剩元数据，原文与下载一律 404。
        self.storage_root = storage_root
        # 留空时按库的类型取值：临时库 8/5 秒，外部库 60/15 秒。
        self.lease_seconds = lease_seconds
        self.heartbeat_seconds = heartbeat_seconds
        self.request_timeout = (
            (model_settings.generator_timeout_seconds + 60) if model_settings else 15
        )
        self.started = time.monotonic()
        self.stack = ExitStack()
        self.processes: dict[str, tuple[subprocess.Popen, BinaryIO]] = {}
        self.log_dir = BACKEND / ".data" / "e2e" / uuid.uuid4().hex[:12]
        self.owner = "demo-owner"
        # 每次运行用新密钥签发令牌：上一轮落在浏览器历史里的令牌对下一次无效。
        self.token_secret = secrets.token_urlsafe(32)

    def say(self, message: str) -> None:
        # 仅步骤提示绕过 pytest 的 Python 输出捕获，服务日志仍正常捕获。
        print(
            f"[{time.monotonic() - self.started:6.1f}s] {message}",
            file=sys.__stdout__ or sys.stdout,
            flush=True,
        )

    def __enter__(self) -> Self:
        try:
            self.directory = pathlib.Path(
                self.stack.enter_context(
                    tempfile.TemporaryDirectory(
                        prefix="ks_runtime_", ignore_cleanup_errors=True
                    )
                )
            ).resolve()
            if not self.directory.is_relative_to(pathlib.Path(tempfile.gettempdir()).resolve()):
                raise RuntimeError("Unexpected runtime directory")
            self.log_dir.mkdir(parents=True)
            if self.model_settings is None:
                self.say(
                    "[mode] Synthetic data + deterministic fixtures; NOT real-model evaluation"
                )
            else:
                self.say("[mode] 真实模型；仅使用本次已批准资料，问题和证据将发送到已配置端点")
            external_database = self.database_url is not None
            if not external_database:
                self.database_url = self.stack.enter_context(isolated_database(self.say))
            # 外部库的心跳与写入都是网络往返，租约按临时库的秒级取值会在发布前过期。
            lease = self.lease_seconds or (60 if external_database else 8)
            heartbeat = self.heartbeat_seconds or (15 if external_database else 5)
            # 临时库或外部库都已在上面确定；此处只收窄类型，不改变运行语义。
            assert self.database_url is not None
            self.env = {
                key: value for key, value in os.environ.items() if not key.startswith("KS_")
            }
            self.env.update(
                {
                    "PYTHONUNBUFFERED": "1",
                    "PYTHONIOENCODING": "utf-8",
                    "PYTHONPATH": str(BACKEND),
                    "NO_PROXY": "127.0.0.1,localhost",
                    "KS_DATABASE_URL": self.database_url,
                    "KS_STORAGE_ROOT": str(self._objects_root(external_database)),
                    "KS_TOKEN_SECRET": self.token_secret,
                    "KS_EMBEDDING_MODEL": "hash-1024",
                    "KS_EMBEDDING_BASE_URL": self.embedding_url,
                    "KS_GENERATOR_BASE_URL": self.generator_url,
                    "KS_GENERATOR_MODEL": "fixture",
                    "KS_PDF_PARSER_PROFILE": "legacy-v1",
                    "KS_LEASE_SECONDS": str(lease),
                    "KS_HEARTBEAT_SECONDS": str(heartbeat),
                    "KS_WORKER_POLL_SECONDS": "0.2",
                }
            )
            if self.model_settings is not None:
                self.env.update(real_model_environment(self.model_settings))
            self.stack.callback(self._stop_all)
            self.say("[migration] Applying schema 0001 -> 0002")
            code = (
                "import sys; from alembic.config import Config; from alembic import command; "
                "c=Config(); c.set_main_option('script_location',sys.argv[1]); command.upgrade(c,'head')"
            )
            with (self.log_dir / "migration.log").open("wb") as output:
                subprocess.run(
                    [sys.executable, "-u", "-c", code, str(BACKEND / "migrations")],
                    env=self.env,
                    cwd=self.directory,
                    stdout=output,
                    stderr=subprocess.STDOUT,
                    check=True,
                    # 外部库经隧道访问时迁移可能明显慢于本机库，留出余量
                    timeout=240,
                )
            self.say("[migration] Schema ready")
            with socket.socket() as listener:
                listener.bind(("127.0.0.1", self.port))
                self.port = listener.getsockname()[1]
            self.base_url = f"http://127.0.0.1:{self.port}"
            self._spawn(
                "api",
                [
                    "-m",
                    "uvicorn",
                    "knowledge.entrypoints.api:app",
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(self.port),
                    "--no-access-log",
                    "--log-level",
                    "warning",
                ],
            )
            self._wait_api()
            self.start_worker()
            self.say(f"[logs] {self.log_dir}")
            return self
        except BaseException:
            self.stack.close()
            raise

    def _objects_root(self, external_database: bool) -> pathlib.Path:
        """对象存储目录：显式指定优先，否则用本次运行的临时卷。

        对象卷必须和数据库一一对应：不同库共用一个卷时，任一侧的孤儿回收
        都会把另一侧还在引用的对象删掉。所以只有明确知道「这一卷就是这个库的」
        的入口（工作台启动器）才传 storage_root，其余场景一律用临时卷。
        """
        if self.storage_root:
            root = pathlib.Path(self.storage_root).expanduser()
        else:
            root = self.directory / "objects"
        root.mkdir(parents=True, exist_ok=True)
        return root.resolve()

    def _spawn(self, name: str, arguments: list[str]) -> None:
        output = (self.log_dir / f"{name}.log").open("ab")
        try:
            process = subprocess.Popen(
                [sys.executable, "-u", *arguments],
                cwd=self.directory,
                env=self.env,
                stdout=output,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0,
            )
        except BaseException:
            output.close()
            raise
        self.processes[name] = process, output
        self.say(f"[{name}] Started independent process PID={process.pid}")

    def _wait_api(self) -> None:
        deadline = time.monotonic() + 90
        last_notice = 0.0
        with httpx.Client(trust_env=False, timeout=1) as client:
            while time.monotonic() < deadline:
                if self.processes["api"][0].poll() is not None:
                    raise RuntimeError(f"API exited; inspect {self.log_dir / 'api.log'}")
                try:
                    response = client.get(self.base_url + "/")
                    if response.status_code == 200:
                        self.say(f"[api] Ready: {self.base_url}/docs")
                        return
                except httpx.TransportError:
                    pass
                if time.monotonic() - last_notice >= 2:
                    self.say("[api] Waiting for startup...")
                    last_notice = time.monotonic()
                time.sleep(0.1)
        raise RuntimeError(f"API startup timed out; inspect {self.log_dir}")

    def start_worker(self) -> None:
        existing = self.processes.get("worker")
        if existing is not None and existing[0].poll() is None:
            return
        if existing is not None:
            self._stop("worker")
        self._spawn("worker", ["-m", "knowledge.entrypoints.worker"])

    def stop_worker(self, *, force: bool = False) -> None:
        self._stop("worker", force=force)

    def _stop(self, name: str, *, force: bool = False) -> None:
        existing = self.processes.pop(name, None)
        if existing is None:
            return
        process, output = existing
        try:
            if process.poll() is None:
                if force:
                    process.kill()
                else:
                    process.terminate()
                try:
                    process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=5)
            self.say(f"[{name}] {'Killed' if force else 'Stopped'} PID={process.pid}")
        finally:
            output.close()

    def _stop_all(self) -> None:
        self._stop("worker")
        self._stop("api")

    def dev_token(self, subject: str | None = None) -> str:
        """本次运行有效的开发令牌。"""
        settings = Settings.model_construct(token_secret=self.token_secret)
        return issue_token(subject or self.owner, settings=settings)

    def workbench_url(self, *, autologin: bool = True) -> str:
        """工作台地址；令牌走 URL 片段，不进请求行也不落盘。"""
        url = f"{self.base_url}/workbench/"
        return f"{url}#t={self.dev_token()}" if autologin else url

    def client(self, subject: str | None = None) -> httpx.Client:
        token = self.dev_token(subject)
        return httpx.Client(
            base_url=self.base_url,
            trust_env=False,
            timeout=self.request_timeout,
            headers={"Authorization": f"Bearer {token}"},
        )

    def __exit__(self, *args: object) -> None:
        self.stack.close()
