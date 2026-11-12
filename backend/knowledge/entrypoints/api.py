"""API 进程入口：装配应用、路由与错误映射，不含业务逻辑。"""

from __future__ import annotations

import time
import uuid
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.responses import Response
from starlette.types import Scope

from knowledge.api import errors
from knowledge.api.v1.routers import chunks, documents, events, jobs, knowledge_bases, qa, search
from knowledge.config import get_settings
from knowledge.logging import get_logger, setup_logging

API_PREFIX = "/api/v1"

log = get_logger("api")


class _RevalidatedStatic(StaticFiles):
    """前端资源允许缓存但每次重新验证。

    默认的 StaticFiles 不发 Cache-Control，浏览器会按「最后修改时间的 10%」自己算新鲜期，
    开发时改完前端普通刷新仍拿到旧模块，表现为「改了页面没变」。ETag 仍在，命中时是 304。
    """

    async def get_response(self, path: str, scope: Scope) -> Response:
        response = await super().get_response(path, scope)
        response.headers["Cache-Control"] = "no-cache"
        return response


def create_app() -> FastAPI:
    setup_logging()
    settings = get_settings()
    if settings.token_secret == "dev-only-secret" and settings.embedding_model != "hash-1024":
        # 身份桩（默认密钥）只允许搭配桩模型用于本地开发；真实模型下默认密钥等于公开门禁。
        raise RuntimeError(
            "检测到默认令牌密钥 KS_TOKEN_SECRET；配置真实模型时必须显式设置该密钥，拒绝启动。"
        )
    app = FastAPI(
        title="Knowledge Service",
        version="0.1.0",
        description="知识检索与智能问答助手：个人知识库、内容检索、带引用的问答。",
    )

    @app.middleware("http")
    async def _request_id(request: Request, call_next):
        request.state.request_id = request.headers.get("X-Request-Id") or uuid.uuid4().hex[:16]
        started = time.perf_counter()
        response = await call_next(request)
        log.info(
            "http_request",
            extra={
                "request_id": request.state.request_id,
                "method": request.method,
                "path": request.url.path,
                "status": response.status_code,
                "duration_ms": round((time.perf_counter() - started) * 1000, 2),
            },
        )
        response.headers["X-Request-Id"] = request.state.request_id
        return response

    errors.install(app)

    frontend = Path(__file__).resolve().parents[3] / "frontend"
    if frontend.is_dir():
        app.mount("/workbench", _RevalidatedStatic(directory=frontend, html=True), name="workbench")

    for module in (knowledge_bases, documents, events, jobs, search, chunks, qa):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get("/", include_in_schema=False)
    def _root() -> JSONResponse:
        return JSONResponse({"data": {"service": "knowledge-service", "docs": "/docs"}})

    return app


app = create_app()
