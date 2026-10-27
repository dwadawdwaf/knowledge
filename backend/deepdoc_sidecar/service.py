"""本机边车协议；单任务、有限资源、无文件路径/原文件名输入。"""

from __future__ import annotations

import asyncio
import hmac
import time
import uuid

import anyio
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict
from starlette.concurrency import run_in_threadpool

from deepdoc_sidecar.runtime import DeepDocRuntime
from knowledge.adapters.deepdoc import MEDIA_TYPES, PROFILE
from knowledge.adapters.parse_process import BoundedParser
from knowledge.config import Settings
from knowledge.domain.errors import ParseFailed, PasswordProtected


class SidecarSettings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="DEEPDOC_", extra="ignore", hide_input_in_errors=True
    )
    token: str = Field(default="", min_length=32, validate_default=True)
    timeout_seconds: float = Field(default=300, gt=0)
    cpu_seconds: float = Field(default=240, gt=0)
    memory_bytes: int = Field(default=4 * 1024**3, gt=0)
    input_bytes: int = Field(default=50 * 1024**2, gt=0)
    output_bytes: int = Field(default=16 * 1024**2, gt=0)
    max_pages: int = Field(default=50, gt=0)
    max_image_pixels: int = Field(default=40_000_000, gt=0)


def create_app(settings: SidecarSettings | None = None) -> FastAPI:
    resolved = settings or SidecarSettings()
    limits = Settings.model_construct(
        parse_timeout_seconds=resolved.timeout_seconds,
        parse_cpu_seconds=resolved.cpu_seconds,
        parse_memory_bytes=resolved.memory_bytes,
        parse_output_bytes=resolved.output_bytes,
    )
    parser = BoundedParser(DeepDocRuntime(resolved.max_pages, resolved.max_image_pixels), limits)
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    slots = asyncio.Semaphore(1)

    @app.get("/health")
    async def health() -> dict:
        # 只表示 HTTP 活着；不把它当作模型加载成功的证据。
        return {"status": "alive", "profile": PROFILE, "models_verified": False}

    @app.post("/v1/parse")
    async def parse(request: Request) -> JSONResponse:
        if not hmac.compare_digest(
            request.headers.get("authorization", "").encode(), ("Bearer " + resolved.token).encode()
        ):
            return JSONResponse({"error": "UNAUTHORIZED"}, status_code=401)
        if request.headers.get("x-parser-profile") != PROFILE:
            return JSONResponse({"error": "PROFILE_MISMATCH"}, status_code=409)
        media = request.headers.get("content-type", "")
        if media not in MEDIA_TYPES:
            return JSONResponse({"error": "UNSUPPORTED_FORMAT"}, status_code=415)
        if slots.locked():
            return JSONResponse({"error": "BUSY"}, status_code=503)
        async with slots:
            request_id = str(uuid.uuid4())
            started = time.monotonic()
            print(f'{{"event":"parse_started","request_id":"{request_id}"}}', flush=True)
            try:
                with anyio.fail_after(30):
                    data = bytearray()
                    async for chunk in request.stream():
                        data.extend(chunk)
                        if len(data) > resolved.input_bytes:
                            return JSONResponse({"error": "FILE_TOO_LARGE"}, status_code=413)
                # 不调用上游服务入口；仅在可终止子进程中使用解析器。
                blocks = await run_in_threadpool(parser.parse, bytes(data), media, "input")
                if not blocks:
                    raise ParseFailed("未提取到正文")
                return JSONResponse(
                    {
                        "profile": PROFILE,
                        "blocks": [
                            {
                                "text": b.text,
                                "page": b.locator.page,
                                "positions": b.locator.to_dict()["positions"],
                                # 按块回传：只给并集会让纯文本块也带上表格/OCR 标签。
                                "warnings": sorted(b.warnings),
                            }
                            for b in blocks
                        ],
                        "warnings": sorted({warning for b in blocks for warning in b.warnings}),
                    }
                )
            except PasswordProtected:
                # 保留主服务现有密码保护错误，不暴露第三方错误正文。
                return JSONResponse({"error": "PASSWORD_PROTECTED"})
            except Exception:  # noqa: BLE001 - 边界不返回上游正文或内部异常。
                return JSONResponse({"error": "PARSE_FAILED"}, status_code=422)
            finally:
                elapsed = round((time.monotonic() - started) * 1000)
                print(
                    f'{{"event":"parse_finished","request_id":"{request_id}",'
                    f'"duration_ms":{elapsed}}}',
                    flush=True,
                )

    return app
