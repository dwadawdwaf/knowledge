"""受信任本机 DeepDoc 服务适配器；不传原文件名，不跟随重定向。"""

from __future__ import annotations

import math
import urllib.request
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, StrictInt, StrictStr, model_validator

from knowledge.config import Settings
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed, PasswordProtected

PROFILE = "deepdoc-v0.22.0-v1"
WARNING_CODES = {
    "deepdoc_ocr_requires_review",
    "deepdoc_table_linearized",
    "deepdoc_figures_not_described",
    "deepdoc_empty_page",
}
MEDIA_TYPES = {"application/pdf", "image/png", "image/jpeg", "image/tiff"}


class _Block(BaseModel):
    model_config = ConfigDict(extra="forbid")
    text: StrictStr = Field(min_length=1)
    page: StrictInt = Field(ge=1)
    positions: list[tuple[StrictInt, float, float, float, float]] = Field(min_length=1)

    @model_validator(mode="after")
    def validate_positions(self) -> _Block:
        for page, left, right, top, bottom in self.positions:
            if (
                page < 1
                or not all(math.isfinite(v) for v in (left, right, top, bottom))
                or not 0 <= left < right
                or not 0 <= top < bottom
            ):
                raise ValueError("无效位置")
        if self.page != self.positions[0][0] or not self.text.strip():
            raise ValueError("无效块")
        return self


class _Result(BaseModel):
    model_config = ConfigDict(extra="forbid")
    profile: Literal["deepdoc-v0.22.0-v1"]
    blocks: list[_Block] = Field(min_length=1)
    warnings: list[StrictStr]


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(
        request, timeout=timeout
    )


class DeepDocParser:
    name = "deepdoc"

    def __init__(self, settings: Settings) -> None:
        parts = urlsplit(settings.deepdoc_url)
        if (
            parts.scheme != "http"
            or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("DeepDoc 端点必须是本机 HTTP origin")
        self.url = settings.deepdoc_url.rstrip("/") + "/v1/parse"
        self.token = settings.deepdoc_token
        self.timeout = settings.deepdoc_timeout_seconds
        self.output_limit = settings.parse_output_bytes

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type in MEDIA_TYPES

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        if not self.token or not self.supports(media_type, filename):
            raise ParseFailed("DeepDoc 配置不完整或文件类型不支持")
        request = urllib.request.Request(
            self.url,
            data=data,
            method="POST",
            headers={
                "Content-Type": media_type,
                "Authorization": f"Bearer {self.token}",
                "X-Parser-Profile": PROFILE,
            },
        )
        try:
            with _open(request, timeout=self.timeout) as response:
                raw = response.read(self.output_limit + 1)
                if len(raw) > self.output_limit:
                    raise ValueError("输出超限")
                if raw == b'{"error":"PASSWORD_PROTECTED"}':
                    raise PasswordProtected("文件已加密")
                result = _Result.model_validate_json(raw)
                if not set(result.warnings) <= WARNING_CODES:
                    raise ValueError("未知警告码")
        except PasswordProtected:
            raise
        except Exception as exc:
            raise ParseFailed("DeepDoc 不可用或解析结果无效") from exc
        return [
            ParsedBlock(
                text=block.text,
                locator=Locator(
                    type="pdf" if media_type == "application/pdf" else "image",
                    page=block.page,
                    block=index,
                    char_start=0,
                    char_end=len(block.text),
                    precision="block",
                    positions=tuple(block.positions),
                ),
                warnings=tuple(result.warnings),
            )
            for index, block in enumerate(result.blocks)
        ]
