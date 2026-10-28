"""启动并校验本机依赖，让一键脚本不必手工先开服务。

数据库由 `LocalRuntime` 每次现起临时实例，不在此模块范围内；
这里只处理**本机向量服务（Ollama）**，并验证向量维度与生成模型确实可用。
远程端点不做拉起，只做连通性与鉴权校验。
"""

from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections.abc import Callable
from urllib.parse import urlsplit

BACKEND = pathlib.Path(__file__).resolve().parents[1]
LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}
OLLAMA_DEFAULT_PORT = 11434
START_TIMEOUT_SECONDS = 45.0


class DependencyError(RuntimeError):
    """依赖缺失或不可用；消息面向使用者，不含密钥与内部堆栈。"""


def _say(message: str) -> None:
    print(message, flush=True)


def _opener_for(url: str) -> urllib.request.OpenerDirector:
    """与运行时同一套出口策略：本机直连，本机代理端口关闭时也直连。

    自检与真正发请求必须用同一策略，否则会出现「自检过了、跑起来失败」。
    """
    from knowledge.adapters.outbound import opener_for

    return opener_for(url)


def _port_open(host: str, port: int, timeout: float = 0.4) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def _post_json(
    url: str,
    payload: dict[str, object],
    *,
    api_key: str | None,
    timeout: float,
    progress: Callable[[str], None] | None = None,
) -> dict[str, object]:
    """POST 一次 JSON；连接层失败时换另一条路由重试一次。

    代理会瞬时断开（`RemoteDisconnected`），一次抖动不该让整个启动失败；
    但 HTTP 状态码是确定结论，不重试。
    """
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    body = json.dumps(payload).encode("utf-8")
    remote = (urlsplit(url).hostname or "") not in LOOPBACK_HOSTS
    routes = [(_opener_for(url), "环境代理"), (urllib.request.build_opener(urllib.request.ProxyHandler({})), "直连")]
    attempts = [(routes[0][0], routes[0][1])] if not remote else routes
    last: Exception | None = None
    for index, (opener, label) in enumerate(attempts):
        if index:
            if progress:
                progress(f"[依赖] {attempts[0][1]}失败，改{label}重试一次")
            time.sleep(0.4)
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with opener.open(request, timeout=timeout) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError:
            raise
        except (urllib.error.URLError, OSError) as exc:
            last = exc
    assert last is not None
    raise last


def _describe_network_error(exc: Exception, what: str, url: str) -> str:
    """把「不可达」拆成能定位的说法：主机、原因类型、可操作提示。"""
    host = urlsplit(url).hostname or url
    reason = getattr(exc, "reason", exc)
    kind = type(reason).__name__
    hint = {
        "gaierror": "域名解析失败，请检查网络或 DNS 设置",
        "ConnectionRefusedError": "对端拒绝连接，服务可能未启动或端口不对",
        "ConnectionResetError": "连接被重置，可能被网络策略拦截",
        "TimeoutError": "连接超时",
        "SSLCertVerificationError": "证书校验失败；若经代理请确认代理根证书",
    }.get(kind, "请检查端点地址、代理与网络")
    return f"{what}不可达：{host}（{hint}；{kind}）。"


def _describe_http_error(exc: urllib.error.HTTPError, what: str) -> str:
    detail = {
        401: "API_KEY 无效",
        402: "账户余额不足",
        403: "无访问权限",
        404: "模型名称或端点路径不存在",
        429: "触发限流",
    }.get(exc.code, "服务返回异常")
    return f"{what}返回 {exc.code}：{detail}"


# --------------------------------------------------------------------------- Ollama


def find_ollama() -> pathlib.Path | None:
    """按环境变量、PATH、常见安装目录依次查找 ollama 可执行文件。"""
    override = os.environ.get("KS_OLLAMA_EXE")
    if override and pathlib.Path(override).is_file():
        return pathlib.Path(override)
    located = shutil.which("ollama")
    if located:
        return pathlib.Path(located)
    candidates = [
        pathlib.Path(os.environ.get("LOCALAPPDATA", "")) / "Programs" / "Ollama" / "ollama.exe",
        pathlib.Path("C:/Program Files/Ollama/ollama.exe"),
        pathlib.Path("D:/Ollama/ollama.exe"),
    ]
    return next((path for path in candidates if path.is_file()), None)


def _local_service(base_url: str, default_port: int) -> tuple[str, int] | None:
    """端点指向本机时返回 (host, port)，指向远程返回 None。"""
    parsed = urlsplit(base_url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        return None
    host = "127.0.0.1" if parsed.hostname in {"localhost", "0.0.0.0"} else parsed.hostname
    return host, parsed.port or default_port


def _list_models(host: str, port: int) -> list[str]:
    opener = _opener_for(f"http://{host}:{port}/api/tags")
    with opener.open(f"http://{host}:{port}/api/tags", timeout=5) as response:
        body = json.loads(response.read().decode("utf-8"))
    return [str(item.get("name", "")) for item in body.get("models", [])]


def _matches(model: str, tags: list[str]) -> bool:
    """配置写 `bge-m3`、Ollama 存成 `bge-m3:latest` 时视为同一个模型。"""
    wanted = model.split(":")[0]
    return any(tag.split(":")[0] == wanted for tag in tags)


def _start_ollama(exe: pathlib.Path) -> None:
    log_dir = BACKEND / ".data" / "deps"
    log_dir.mkdir(parents=True, exist_ok=True)
    output = (log_dir / "ollama.log").open("ab")
    try:
        subprocess.Popen(
            [str(exe), "serve"],
            cwd=str(exe.parent),
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                if os.name == "nt"
                else 0
            ),
        )
    finally:
        output.close()


def ensure_ollama(
    base_url: str,
    model: str,
    *,
    pull_missing: bool = False,
    progress: Callable[[str], None] = _say,
) -> None:
    """确保本机 Ollama 已运行且装有指定模型；远程端点直接跳过。"""
    target = _local_service(base_url, OLLAMA_DEFAULT_PORT)
    if target is None:
        progress(f"[依赖] 向量端点非本机（{urlsplit(base_url).hostname}），不代为启动")
        return
    host, port = target
    if _port_open(host, port):
        progress(f"[依赖] 向量服务已在运行：{host}:{port}")
    else:
        exe = find_ollama()
        if exe is None:
            raise DependencyError(
                "未找到 ollama，无法启动本机向量服务。请安装 Ollama 后重试，"
                "或把 KS_EMBEDDING_BASE_URL 指向可用的向量服务。"
            )
        progress(f"[依赖] 启动向量服务：{exe} serve")
        _start_ollama(exe)
        deadline = time.monotonic() + START_TIMEOUT_SECONDS
        while time.monotonic() < deadline:
            if _port_open(host, port):
                break
            time.sleep(0.3)
        else:
            raise DependencyError(
                f"向量服务启动超时（{START_TIMEOUT_SECONDS:.0f}s 内 {host}:{port} 未监听）。"
                f"可查看 backend/.data/deps/ollama.log。"
            )
        progress(f"[依赖] 向量服务已就绪：{host}:{port}")

    try:
        tags = _list_models(host, port)
    except (urllib.error.URLError, OSError, ValueError) as exc:
        raise DependencyError("向量服务已监听但未响应，请检查 ollama 进程状态。") from exc

    if _matches(model, tags):
        progress(f"[依赖] 向量模型已就绪：{model}")
        return
    if not pull_missing:
        raise DependencyError(
            f"向量模型 {model} 未安装。执行 `ollama pull {model}` 后重试，"
            f"或加 -PullMissing 让脚本自动拉取。"
        )
    exe = find_ollama()
    if exe is None:  # pragma: no cover - 极少出现，服务在跑但找不到命令
        raise DependencyError(f"找不到 ollama 命令，无法拉取模型 {model}。")
    progress(f"[依赖] 拉取向量模型 {model}（约 1.2 GB，首次较慢）")
    result = subprocess.run([str(exe), "pull", model], check=False)
    if result.returncode != 0:
        raise DependencyError(f"拉取模型 {model} 失败，请检查网络后重试。")


# --------------------------------------------------------------------------- 校验


def verify_embedding(
    base_url: str,
    model: str,
    dimension: int,
    *,
    api_key: str | None = None,
    dimensions: int | None = None,
    timeout: float = 30.0,
    progress: Callable[[str], None] | None = None,
) -> int:
    """真实调用一次 /embeddings，返回实际维度；维度不符直接报错。"""
    payload: dict[str, object] = {"model": model, "input": ["就绪检查"]}
    if dimensions is not None:
        payload["dimensions"] = dimensions
    try:
        body = _post_json(
            f"{base_url.rstrip('/')}/embeddings",
            payload,
            api_key=api_key,
            timeout=timeout,
            progress=progress,
        )
    except urllib.error.HTTPError as exc:
        raise DependencyError(_describe_http_error(exc, "向量服务")) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DependencyError(
            _describe_network_error(exc, "向量服务", f"{base_url.rstrip('/')}/embeddings")
        ) from exc
    except TimeoutError as exc:
        raise DependencyError(f"向量服务在 {timeout:.0f}s 内未返回。") from exc
    try:
        vector = body["data"][0]["embedding"]  # type: ignore[index]
    except (KeyError, IndexError, TypeError) as exc:
        raise DependencyError("向量服务响应结构不符合 OpenAI 兼容格式。") from exc
    actual = len(vector)
    if actual != dimension:
        raise DependencyError(
            f"向量维度不符：配置 {dimension} 维，模型实际返回 {actual} 维。"
            f"请改 KS_EMBEDDING_MODEL 或重建索引，不允许混用维度。"
        )
    return actual


def verify_generator(
    base_url: str,
    model: str,
    *,
    api_key: str | None = None,
    timeout: float = 30.0,
    progress: Callable[[str], None] | None = None,
) -> None:
    """用 1 个 token 的请求确认模型名、密钥与余额都正常。"""
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": "ping"}],
        "max_tokens": 1,
        "temperature": 0,
    }
    try:
        body = _post_json(
            f"{base_url.rstrip('/')}/chat/completions",
            payload,
            api_key=api_key,
            timeout=timeout,
            progress=progress,
        )
    except urllib.error.HTTPError as exc:
        raise DependencyError(_describe_http_error(exc, "生成服务")) from exc
    except (urllib.error.URLError, OSError) as exc:
        raise DependencyError(
            _describe_network_error(exc, "生成服务", f"{base_url.rstrip('/')}/chat/completions")
        ) from exc
    except TimeoutError as exc:
        raise DependencyError(f"生成服务在 {timeout:.0f}s 内未返回。") from exc
    if not body.get("choices"):
        raise DependencyError("生成服务响应缺少 choices，模型可能不可用。")


DEEPDOC_PROFILE = "deepdoc-v0.22.0-v1"
COMPOSE_FILE = BACKEND.parent / "deploy" / "deepdoc" / "compose.yaml"
DEEPDOC_READY_SECONDS = 180.0


DOCKER_FALLBACK_DIRS = (
    pathlib.Path("D:/Docker/Desktop/resources/bin"),
    pathlib.Path(r"C:/Program Files/Docker/Docker/resources/bin"),
)
DOCKER_DESKTOP_EXES = (
    pathlib.Path("D:/Docker/Desktop/Docker Desktop.exe"),
    pathlib.Path(r"C:/Program Files/Docker/Docker/Docker Desktop.exe"),
)
DOCKER_START_SECONDS = 180.0


def find_docker() -> str | None:
    """docker 通常在 PATH；本机 Docker Desktop 装在 D 盘，需要补一个候选目录。"""
    found = shutil.which("docker")
    if found:
        return found
    for directory in DOCKER_FALLBACK_DIRS:
        for name in ("docker.exe", "docker"):
            candidate = directory / name
            if candidate.is_file():
                return str(candidate)
    return None


def _docker_engine_running(docker: str) -> bool:
    try:
        completed = subprocess.run(
            [docker, "info", "--format", "{{.OSType}}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
        )
    except (OSError, subprocess.TimeoutExpired):
        return False
    return completed.returncode == 0


def start_docker_engine(
    docker: str, *, progress: Callable[[str], None] = _say
) -> bool:
    """引擎未运行时尝试拉起 Docker Desktop；返回是否就绪。"""
    exe = next((path for path in DOCKER_DESKTOP_EXES if path.is_file()), None)
    if exe is None:
        return False
    progress(f"[依赖] 启动 Docker Desktop：{exe}")
    creationflags = 0
    if os.name == "nt":
        creationflags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS
    try:
        subprocess.Popen(
            [str(exe)],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=creationflags,
        )
    except OSError:
        return False
    deadline = time.monotonic() + DOCKER_START_SECONDS
    while time.monotonic() < deadline:
        if _docker_engine_running(docker):
            progress("[依赖] Docker 引擎已就绪")
            return True
        time.sleep(3)
    return False


def _deepdoc_healthy(url: str) -> bool:
    try:
        request = urllib.request.Request(url.rstrip("/") + "/health")
        with _opener_for(request.full_url).open(request, timeout=3) as response:
            return json.loads(response.read().decode("utf-8")).get("status") == "alive"
    except Exception:  # noqa: BLE001 - 探活失败一律按未就绪处理
        return False


def ensure_deepdoc(
    settings, *, progress: Callable[[str], None] = _say
) -> None:
    """使用 DeepDoc 解析时，边车同样是前置依赖，未运行则拉起容器。"""
    profile = getattr(settings, "document_parser_profile", "legacy-v1")
    if profile != DEEPDOC_PROFILE:
        return
    url = getattr(settings, "deepdoc_url", "") or ""
    if not url:
        raise DependencyError("选择了 DeepDoc 解析但缺少 KS_DEEPDOC_URL。")
    if not getattr(settings, "deepdoc_token", None):
        raise DependencyError("选择了 DeepDoc 解析但缺少 KS_DEEPDOC_TOKEN。")
    if _deepdoc_healthy(url):
        progress(f"[依赖] DeepDoc 边车已就绪：{url}")
        return

    docker = find_docker()
    if docker is None:
        raise DependencyError(
            "选择了 DeepDoc 解析，但边车未运行且未找到 docker。"
            "请先运行 start-deepdoc.ps1，或把 backend/.env 的 KS_DOCUMENT_PARSER_PROFILE 改回 legacy-v1。"
        )
    if not _docker_engine_running(docker) and not start_docker_engine(
        docker, progress=progress
    ):
        raise DependencyError(
            "选择了 DeepDoc 解析，但 Docker 引擎未能启动。请手动启动 Docker Desktop 后重试；"
            "不打算用 DeepDoc 时，把 backend/.env 的 KS_DOCUMENT_PARSER_PROFILE 改回 legacy-v1。"
        )
    if not COMPOSE_FILE.is_file():
        raise DependencyError(f"缺少边车编排文件：{COMPOSE_FILE}")
    progress(f"[依赖] 启动 DeepDoc 边车：{COMPOSE_FILE.name}（首次启动较慢）")
    env = {**os.environ, "DEEPDOC_TOKEN": str(settings.deepdoc_token)}
    try:
        completed = subprocess.run(
            [docker, "compose", "-p", "ks-deepdoc", "-f", str(COMPOSE_FILE), "up", "-d"],
            cwd=str(BACKEND.parent),
            env=env,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=600,
        )
    except subprocess.TimeoutExpired as exc:
        raise DependencyError("启动 DeepDoc 边车超时，请查看 docker compose 日志。") from exc
    if completed.returncode != 0:
        tail = (completed.stderr or completed.stdout or "").strip().splitlines()[-1:]
        raise DependencyError(
            "启动 DeepDoc 边车失败：" + (tail[0] if tail else "docker compose 返回非零")
        )

    deadline = time.monotonic() + DEEPDOC_READY_SECONDS
    while time.monotonic() < deadline:
        if _deepdoc_healthy(url):
            progress(f"[依赖] DeepDoc 边车就绪：{url}")
            return
        time.sleep(2)
    raise DependencyError(
        f"DeepDoc 边车在 {DEEPDOC_READY_SECONDS:.0f}s 内未就绪，请查看容器日志。"
    )


def ensure_database(
    settings, *, progress: Callable[[str], None] = _say
) -> None:
    """数据库指向本机端口但无人监听时，按配置建立 SSH 隧道。"""
    parsed = urlsplit(settings.database_url)
    if parsed.hostname not in LOOPBACK_HOSTS:
        return
    port = parsed.port or 5432
    if _port_open(parsed.hostname, port):
        progress(f"[依赖] 数据库端口已就绪：{parsed.hostname}:{port}")
        return
    target = settings.tunnel_ssh
    if not target:
        raise DependencyError(
            f"数据库 {parsed.hostname}:{port} 未监听，且未配置 KS_TUNNEL_SSH。"
            "请手动建立隧道，或写入 KS_TUNNEL_SSH=<user>@<host>。"
        )
    # 交给守护进程维持：链路被重置后自动重连，启动器退出也不影响。
    command = [sys.executable, "-m", "scripts.db_tunnel"]
    progress(f"[依赖] 建立数据库隧道：{parsed.hostname}:{port} -> {target}:5432")
    log_dir = BACKEND / ".data" / "deps"
    log_dir.mkdir(parents=True, exist_ok=True)
    output = (log_dir / "tunnel.log").open("ab")
    try:
        subprocess.Popen(
            command,
            cwd=str(BACKEND),
            stdin=subprocess.DEVNULL,
            stdout=output,
            stderr=subprocess.STDOUT,
            creationflags=(
                subprocess.CREATE_NO_WINDOW | subprocess.DETACHED_PROCESS
                if os.name == "nt"
                else 0
            ),
        )
    finally:
        output.close()
    deadline = time.monotonic() + START_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        if _port_open(parsed.hostname, port):
            progress(f"[依赖] 隧道就绪：{parsed.hostname}:{port}")
            return
        time.sleep(0.3)
    raise DependencyError(
        f"数据库隧道未就绪（{START_TIMEOUT_SECONDS:.0f}s 内端口未监听）；"
        "请检查 SSH 密钥与服务器状态，日志见 backend/.data/deps/tunnel.log。"
    )


def ensure_dependencies(
    settings,
    *,
    pull_missing: bool = False,
    check_generator: bool = True,
    progress: Callable[[str], None] = _say,
) -> None:
    """启动本机依赖并验证两个模型端点；失败抛 DependencyError。"""
    ensure_database(settings, progress=progress)
    ensure_deepdoc(settings, progress=progress)
    # 代理开着又退出是这台机器的常态；明确说出来，不靠看日志才发现走了直连。
    from knowledge.adapters.outbound import dead_local_proxy

    dead = dead_local_proxy()
    if dead:
        progress(f"[依赖] 环境代理 {dead} 未监听，远端请求改直连")
    base_url = settings.embedding_base_url or ""
    if not base_url:
        raise DependencyError("缺少向量端点 KS_EMBEDDING_BASE_URL。")
    ensure_ollama(
        base_url, settings.embedding_model, pull_missing=pull_missing, progress=progress
    )
    dimension = verify_embedding(
        base_url,
        settings.embedding_model,
        settings.embedding_dim,
        api_key=settings.embedding_api_key,
        dimensions=settings.embedding_dimensions,
        progress=progress,
    )
    progress(f"[依赖] 向量自检通过：{settings.embedding_model}（{dimension} 维）")

    if not check_generator:
        return
    generator_url = settings.generator_base_url or ""
    if not generator_url:
        raise DependencyError("缺少生成端点 KS_GENERATOR_BASE_URL。")
    verify_generator(
        generator_url,
        settings.generator_model,
        api_key=settings.generator_api_key,
        timeout=settings.generator_timeout_seconds,
        progress=progress,
    )
    progress(f"[依赖] 生成自检通过：{settings.generator_model}")


def main() -> int:
    sys.path.insert(0, str(BACKEND))
    from scripts.real_model import load_real_settings

    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--pull-missing", action="store_true", help="缺少向量模型时自动拉取")
    parser.add_argument(
        "--skip-generator", action="store_true", help="不校验生成端点（省一次调用）"
    )
    args = parser.parse_args()
    try:
        settings = load_real_settings()
        ensure_dependencies(
            settings,
            pull_missing=args.pull_missing,
            check_generator=not args.skip_generator,
        )
        _say("[依赖] 全部就绪")
        return 0
    except DependencyError as exc:
        _say(f"[依赖] 失败：{exc}")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
