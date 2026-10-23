"""把领域异常映射为 HTTP 响应。业务层不感知状态码。"""

from __future__ import annotations

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from knowledge.domain.errors import DomainError, ValidationFailed
from knowledge.logging import get_logger

log = get_logger("api")


def _body(code: str, message: str, retryable: bool, request_id: str) -> dict:
    return {
        "error": {"code": code, "message": message, "retryable": retryable},
        "meta": {"request_id": request_id},
    }


def install(app: FastAPI) -> None:
    @app.exception_handler(DomainError)
    async def _domain_error(request: Request, exc: DomainError) -> JSONResponse:
        log.info(
            "request_failed",
            extra={"request_id": _rid(request), "code": exc.code, "status": exc.http_status},
        )
        return JSONResponse(
            status_code=exc.http_status,
            content=_body(exc.code, exc.message, exc.retryable, _rid(request)),
        )

    @app.exception_handler(RequestValidationError)
    async def _validation_error(request: Request, exc: RequestValidationError) -> JSONResponse:
        detail = exc.errors()[0] if exc.errors() else {}
        message = detail.get("msg", "请求参数不合法")
        return JSONResponse(
            status_code=ValidationFailed.http_status,
            content=_body(ValidationFailed.code, str(message), False, _rid(request)),
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        log.error(
            "request_unhandled",
            extra={"request_id": _rid(request), "exception_type": type(exc).__name__},
            exc_info=exc,
        )
        return JSONResponse(
            status_code=500,
            content=_body("INTERNAL_ERROR", "服务内部错误", False, _rid(request)),
        )


def _rid(request: Request) -> str:
    return getattr(request.state, "request_id", "")
