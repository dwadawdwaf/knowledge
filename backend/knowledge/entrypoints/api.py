"""API 进程入口：装配应用、路由与错误映射，不含业务逻辑。"""

from __future__ import annotations

import time
import uuid

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

from knowledge.api import errors
from knowledge.api.v1.routers import chunks, documents, jobs, knowledge_bases, qa, search
from knowledge.logging import get_logger, setup_logging

API_PREFIX = "/api/v1"

log = get_logger("api")


def create_app() -> FastAPI:
    setup_logging()
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

    for module in (knowledge_bases, documents, jobs, search, chunks, qa):
        app.include_router(module.router, prefix=API_PREFIX)

    @app.get("/", include_in_schema=False)
    def _root() -> JSONResponse:
        return JSONResponse({"data": {"service": "knowledge-service", "docs": "/docs"}})

    return app


app = create_app()
