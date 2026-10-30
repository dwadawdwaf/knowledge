"""本机MinerU 4.1 file-parse接口；有界ZIP读取，不信任返回URL或归档路径。"""

from __future__ import annotations

import io
import json
import re
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from html.parser import HTMLParser
from urllib.parse import urlsplit

from knowledge.adapters.deepdoc import _open
from knowledge.config import Settings
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed

PROFILE = "mineru-api4.1-v1"
EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
}


class HtmlText(HTMLParser):
    """仅保留文字及行列分隔，不执行HTML或读取引用资源。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"tr", "br", "p"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag in {"tr", "p"}:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def normalize_archive(data: bytes, media: str, limit: int) -> list[ParsedBlock]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2048 or sum(e.file_size for e in entries) > limit:
            raise ValueError("解压预算超限")
        names = [e.filename for e in entries]
        if len(names) != len(set(names)) or names.count("structured_content.json") != 1:
            raise ValueError("结构结果缺失或重复")
        # 不extract，也不读取images、其他成员或任何磁盘路径。
        content = json.loads(archive.read("structured_content.json"))
    if content.get("is_full_document") is not True or not isinstance(content.get("pages"), list):
        raise ValueError("不接受截断文档")
    producer = content.get("metadata", {}).get("producer", {})
    if producer.get("name") != "mineru" or not str(producer.get("version", "")).startswith("4.1."):
        raise ValueError("解析协议版本不匹配")
    result: list[ParsedBlock] = []
    seen = set()
    skipped = False
    for page in content["pages"]:
        index = page["page_idx"]
        if type(index) is not int or index < 0 or index in seen:
            raise ValueError("页索引无效")
        seen.add(index)
        if not isinstance(page["blocks"], list):
            raise TypeError("块结构无效")
        for block in page["blocks"]:
            value = block.get("content")
            if not isinstance(value, str) or not value.strip():
                skipped = True
                continue
            warnings = {"mineru_requires_review", "mineru_geometry_unavailable"}
            if "table" in str(block.get("type", "")) or re.search(
                r"</?(?:table|thead|tbody|tr|td|th|p|div|span|br|script|style|h[1-6])(?:\s[^>]*|\s*/?)>",
                value,
                re.IGNORECASE,
            ):
                parser = HtmlText()
                parser.feed(value)
                value = "".join(parser.parts).strip()
                warnings.add("mineru_table_linearized")
            paginated = media == "application/pdf" or media.startswith("image/")
            if not paginated:
                warnings.add("mineru_native_location_unavailable")
            if not value.strip():
                skipped = True
                continue
            result.append(
                ParsedBlock(
                    value,
                    Locator(
                        type="pdf"
                        if media == "application/pdf"
                        else "image"
                        if paginated
                        else EXTENSIONS[media][1:],
                        page=index + 1 if paginated else None,
                        block=len(result),
                        char_start=0,
                        char_end=len(value),
                        precision="page" if paginated else "block",
                    ),
                    tuple(sorted(warnings)),
                )
            )
    if not result:
        raise ValueError("没有可用正文")
    if skipped:
        first = result[0]
        result[0] = ParsedBlock(
            first.text, first.locator, (*first.warnings, "mineru_content_skipped")
        )
    return result


class MinerUParser:
    name = "mineru"

    def __init__(self, settings: Settings) -> None:
        parts = urlsplit(settings.mineru_url)
        if (
            parts.scheme != "http"
            or parts.hostname not in {"localhost", "127.0.0.1", "::1"}
            or parts.username
            or parts.password
            or parts.path not in {"", "/"}
            or parts.query
            or parts.fragment
        ):
            raise ValueError("MinerU端点必须是本机HTTP origin")
        if not 0 < settings.mineru_timeout_seconds <= 3600:
            raise ValueError("MinerU等待预算必须在0至3600秒内")
        self.url = settings.mineru_url.rstrip("/")
        self.token = settings.mineru_token
        self.timeout = settings.mineru_timeout_seconds
        self.limit = settings.parse_output_bytes

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type in EXTENSIONS

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        if not self.supports(media_type, filename):
            raise ParseFailed("MinerU不支持该文件类型")
        request_id = uuid.uuid4().hex
        boundary = "ks" + uuid.uuid4().hex
        fields = {
            "tier": "basic",
            "ocr_mode": "auto",
            "page_range": "all",
            "output_formats": "default",
            "wait_timeout": "0",
            "request_id": request_id,
        }
        body = bytearray()
        for key, value in fields.items():
            body.extend(
                f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode()
            )
        body.extend(
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; filename="input{EXTENSIONS[media_type]}"\r\nContent-Type: {media_type}\r\n\r\n'.encode()
        )
        body.extend(data)
        body.extend(f"\r\n--{boundary}--\r\n".encode())
        headers = {"Content-Type": f"multipart/form-data; boundary={boundary}"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        request = urllib.request.Request(
            self.url + "/v1/file_parse", data=bytes(body), headers=headers
        )
        deadline = time.monotonic() + self.timeout
        try:
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ValueError("等待超限")
                with _open(request, timeout=remaining) as response:
                    raw = response.read(self.limit + 1)
                    if len(raw) > self.limit or time.monotonic() >= deadline:
                        raise ValueError("输出超限")
                    if raw.startswith(b"PK"):
                        return normalize_archive(raw, media_type, self.limit)
                    if request.full_url.endswith("/result"):
                        raise ValueError("结果不是ZIP")
                    status = json.loads(raw)
                    if status.get("request_id") != request_id:
                        raise ValueError("回执不匹配")
                    state = status.get("status")
                    if state not in {
                        "prepared",
                        "submitting",
                        "waiting-file",
                        "pending",
                        "running",
                        "completed",
                    }:
                        raise ValueError("解析失败或结果不确定")
                path = f"/v1/file_parse/{request_id}" + ("/result" if state == "completed" else "")
                request = urllib.request.Request(
                    self.url + path,
                    headers={k: v for k, v in headers.items() if k == "Authorization"},
                )
                if state != "completed":
                    time.sleep(min(1, max(0, deadline - time.monotonic())))
        except urllib.error.HTTPError as exc:
            message = (
                "MinerU接口不匹配，需要4.1文件解析服务"
                if exc.code == 404
                else "MinerU不可用或解析结果无效"
            )
            raise ParseFailed(message) from exc
        except urllib.error.URLError as exc:
            raise ParseFailed("MinerU服务未就绪，请先启动本机解析服务") from exc
        except Exception as exc:
            raise ParseFailed("MinerU不可用或解析结果无效") from exc
