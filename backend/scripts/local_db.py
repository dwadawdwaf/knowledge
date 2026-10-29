"""本机数据库：用内置 PostgreSQL 的二进制直接管理一个本地集群，不依赖远端服务器。

三种来源按顺序尝试：
1. 已有集群目录（`backend/.data/pgdata` 或用户目录）→ 直接启动 postgres 进程；
2. 没有集群 → 用 initdb 初始化一个，再启动；
3. 已经在跑 → 直接复用。

数据目录放在本机，进程退出后仍在。就绪判据是「能执行 select 1」，
不是「端口在听」——端口刚绑定时的握手失败不该被当成可用。
"""

from __future__ import annotations

import os
import pathlib
import subprocess
import time
from collections.abc import Callable

BACKEND = pathlib.Path(__file__).resolve().parents[1]
DB_URL_FILE = BACKEND / ".data" / "db_url"
# 项目内集群目录：仓库自带、已验证可启动；没有时退回用户目录
PROJECT_PGDATA = BACKEND / ".data" / "pgdata"
DB_NAME = "knowledge"
DEFAULT_PORT = int(os.environ.get("KS_LOCAL_DB_PORT", "55433"))
WAIT_SECONDS = 120.0
# Windows 下所有子进程都不弹控制台：启动器是在用户窗口里跑的，弹窗很打扰。
# 只用 CREATE_NO_WINDOW，不要加 DETACHED_PROCESS——后者让 postmaster 彻底没有控制台，
# 而 PostgreSQL 每个连接都会 fork 一个 backend 进程，每个 backend 都会自己申请控制台窗口，
# 于是每建立一次连接就闪一个窗，导数据时能把屏幕刷满。
_NO_WINDOW = subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0
# initdb 失败后记一笔：换机重装才需要重试，不要每次启动都弹一个黑框
INITDB_MARKER = BACKEND / ".data" / "deps" / "initdb_failed"


class LocalDatabaseError(RuntimeError):
    """本机库无法启动；消息里带可执行的下一步。"""


def bin_dir() -> pathlib.Path:
    """内置 PostgreSQL 的可执行文件目录。"""
    from embedded_postgres import _commands

    return pathlib.Path(_commands.POSTGRES_BIN_PATH)


def data_dir() -> pathlib.Path:
    """集群目录：优先项目内已验证可用的那个，其次用户目录。"""
    override = os.environ.get("KS_LOCAL_DB_DIR")
    if override:
        return pathlib.Path(override).expanduser().resolve()
    if (PROJECT_PGDATA / "PG_VERSION").is_file():
        return PROJECT_PGDATA
    base = os.environ.get("LOCALAPPDATA") or os.environ.get("XDG_DATA_HOME")
    return pathlib.Path(base) / "ks-postgres" if base else pathlib.Path.home() / ".ks-postgres"


def database_url(port: int = DEFAULT_PORT, name: str = DB_NAME) -> str:
    return f"postgresql+psycopg://postgres@127.0.0.1:{port}/{name}"


def can_connect(url: str, timeout: int = 3) -> bool:
    """真实可用性：连上去执行一次查询，而不是只看端口。"""
    import psycopg

    dsn = url.replace("postgresql+psycopg://", "postgresql://")
    try:
        with psycopg.connect(dsn, connect_timeout=timeout) as connection, connection.cursor() as cursor:
            cursor.execute("select 1")
            cursor.fetchone()
        return True
    except Exception:  # noqa: BLE001 - 连接失败一律视为不可用
        return False


def instance_alive(directory: pathlib.Path | None = None) -> bool:
    """数据目录里是否真有一个活着的 postmaster（不能只看 pid 文件在不在）。"""
    import psutil

    directory = directory or data_dir()
    pid_file = directory / "postmaster.pid"
    if not pid_file.is_file():
        return False
    try:
        pid = int(pid_file.read_text(encoding="utf-8").splitlines()[0])
    except (ValueError, IndexError, OSError):
        return False
    try:
        process = psutil.Process(pid)
    except psutil.Error:
        return False
    return "postgres" in (process.name() or "").lower()


def stop_instance(
    directory: pathlib.Path | None = None,
    *,
    wait_seconds: float = 40.0,
) -> bool:
    """用 pg_ctl 做一次干净关闭。

    直接 terminate 会留下共享内存块，下一个实例会报
    「pre-existing shared memory block is still in use」而起不来。
    也**不要手动删 postmaster.pid**：运行中的实例发现锁文件消失会立即自杀。
    """
    directory = directory or data_dir()
    if not instance_alive(directory):
        return False

    binary = bin_dir() / "pg_ctl.exe"
    if not binary.is_file():
        binary = bin_dir() / "pg_ctl"
    subprocess.run(
        [str(binary), "-D", str(directory), "stop", "-m", "fast", "-w", "-t",
         str(int(wait_seconds))],
        check=False,
        capture_output=True,
        creationflags=_NO_WINDOW,
    )
    deadline = time.monotonic() + wait_seconds
    while time.monotonic() < deadline:
        if not instance_alive(directory):
            return True
        time.sleep(0.3)
    return False


def ensure(
    progress: Callable[[str], None] = lambda message: None,
    *,
    port: int = DEFAULT_PORT,
    wait_seconds: float = WAIT_SECONDS,
) -> str:
    """保证本机库可用，返回可直接给 SQLAlchemy 用的 URL。"""
    url = database_url(port)
    # 就绪探测要打在维护库上：应用库可能还没建出来
    maintenance = database_url(port, "postgres")

    if can_connect(url):
        progress(f"[本机库] 已在运行：127.0.0.1:{port}/{DB_NAME}")
        _write_url(url)
        return url

    directory = data_dir()
    if not (directory / "PG_VERSION").is_file():
        progress(f"[本机库] 初始化数据目录：{directory}")
        _initdb(directory)

    if instance_alive(directory):
        # 已经有一个实例在跑（可能是上次启动的），先等它就绪，别去动 pid 文件
        progress(f"[本机库] 已有实例在运行，等待就绪：127.0.0.1:{port}")
        short = time.monotonic() + 15
        while time.monotonic() < short and not can_connect(maintenance):
            time.sleep(0.5)
        if not can_connect(maintenance):
            # 端口不一致或处于关闭过程中：干净关掉再起一个
            progress("[本机库] 已有实例不可用，重启一个")
            stop_instance(directory)
            _spawn_postgres(directory, port)
    else:
        # 不手动删 postmaster.pid：PostgreSQL 启动时会自行处理陈旧锁文件，
        # 而运行中的实例一旦发现锁文件消失会立即关闭。
        progress(f"[本机库] 启动 PostgreSQL：{directory}（端口 {port}）")
        _spawn_postgres(directory, port)

    deadline = time.monotonic() + wait_seconds
    last_notice = 0.0
    prepared = False
    while time.monotonic() < deadline:
        if can_connect(maintenance):
            if not prepared:
                _prepare_database(port)
                prepared = True
            if can_connect(url):
                progress(f"[本机库] 就绪：127.0.0.1:{port}/{DB_NAME}")
                _write_url(url)
                return url
        now = time.monotonic()
        if now - last_notice >= 5:
            progress("[本机库] 等待数据库就绪…")
            last_notice = now
        time.sleep(0.5)
    raise LocalDatabaseError(
        f"本机数据库在 {wait_seconds:.0f}s 内未就绪；日志见 backend/.data/deps/local_db.log"
    )


def _write_url(url: str) -> None:
    DB_URL_FILE.parent.mkdir(parents=True, exist_ok=True)
    DB_URL_FILE.write_text(url, encoding="utf-8")


def _initdb(directory: pathlib.Path) -> None:
    """初始化集群；中文 Windows 上的编码问题在这里转成可读的提示。"""
    if INITDB_MARKER.is_file():
        raise LocalDatabaseError(
            "本机 PostgreSQL 初始化此前已失败（记录见 backend/.data/deps/initdb_failed），"
            "不再重复尝试。\n"
            "可选其一：\n"
            "  1) 安装 PostgreSQL 15+ 建好库，把连接串写进 backend/.env 的 KS_DATABASE_URL；\n"
            "  2) 指定一个已初始化的数据目录：KS_LOCAL_DB_DIR=<目录>；\n"
            "  3) 确认修好后删除该记录文件再启动。"
        )
    directory.parent.mkdir(parents=True, exist_ok=True)
    binary = bin_dir() / "initdb.exe"
    if not binary.is_file():
        binary = bin_dir() / "initdb"
    env = {**os.environ, "LC_ALL": "C", "LANG": "C"}
    completed = subprocess.run(
        [
            str(binary), "-D", str(directory), "--auth=trust", "--auth-local=trust",
            "--encoding=utf8", "--locale=C", "-U", "postgres",
        ],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        creationflags=_NO_WINDOW,
    )
    if completed.returncode != 0:
        INITDB_MARKER.parent.mkdir(parents=True, exist_ok=True)
        INITDB_MARKER.write_text((completed.stderr or "").strip()[-400:], encoding="utf-8")
        lines = (completed.stderr or "").strip().splitlines()
        detail = lines[-1] if lines else "未知错误"
        raise LocalDatabaseError(
            f"本机 PostgreSQL 初始化失败：{detail}\n"
            "常见原因是系统代码页不是 UTF-8。可选其一：\n"
            "  1) 安装 PostgreSQL 15+ 建好库，把连接串写进 backend/.env 的 KS_DATABASE_URL；\n"
            "  2) 指定一个已初始化的数据目录：KS_LOCAL_DB_DIR=<目录>。"
        )


def _spawn_postgres(directory: pathlib.Path, port: int) -> None:
    binary = bin_dir() / "postgres.exe"
    if not binary.is_file():
        binary = bin_dir() / "postgres"
    log_dir = BACKEND / ".data" / "deps"
    log_dir.mkdir(parents=True, exist_ok=True)
    output = (log_dir / "local_db.log").open("ab")
    try:
        subprocess.Popen(
            [str(binary), "-D", str(directory), "-p", str(port), "-c", "listen_addresses=127.0.0.1"],
            cwd=str(BACKEND),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=_NO_WINDOW,
        )
    finally:
        output.close()


def _prepare_database(port: int) -> None:
    """确保应用库与扩展存在（首次启动时库还不存在）。"""
    import psycopg

    admin = database_url(port, "postgres").replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(admin, connect_timeout=5) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1 FROM pg_database WHERE datname = %s", (DB_NAME,))
            if cursor.fetchone() is None:
                cursor.execute(f'CREATE DATABASE "{DB_NAME}"')
    target = database_url(port).replace("postgresql+psycopg://", "postgresql://")
    with psycopg.connect(target, connect_timeout=5) as connection:
        connection.autocommit = True
        with connection.cursor() as cursor:
            for extension in ("vector", "pg_trgm"):
                cursor.execute(f"CREATE EXTENSION IF NOT EXISTS {extension}")
