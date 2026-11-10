"""本机MinerU 4.1 file-parse接口；有界ZIP读取，不信任返回URL或归档路径。"""

from __future__ import annotations

import io
import json
import math
import re
import time
import urllib.error
import urllib.request
import uuid
import zipfile
from collections.abc import Callable
from dataclasses import replace
from html.parser import HTMLParser
from urllib.parse import urlsplit

from knowledge.adapters.parser import DOC, DOCX, detect_media_type
from knowledge.config import Settings
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed

PROFILE = "mineru-api4.1-v1"
EXTENSIONS = {
    "application/pdf": ".pdf",
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/tiff": ".tiff",
    DOC: ".doc",
    DOCX: ".docx",
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": ".xlsx",
    "application/vnd.openxmlformats-officedocument.presentationml.presentation": ".pptx",
    "text/csv": ".csv",
}
_OFFICE_IMAGE_PATH = re.compile(r"images/[A-Za-z0-9_.-]+\.[A-Za-z0-9]+")
_MAX_OFFICE_IMAGES = 32


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def _open(request: urllib.request.Request, *, timeout: float):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect()).open(
        request, timeout=timeout
    )


class HtmlText(HTMLParser):
    """仅保留文字及行列分隔，不执行HTML或读取引用资源。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.images: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag == "img" and not self.hidden:
            self.images.append(dict(attrs).get("src") or "")
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


def _pdf_boxes(middle: object, producer: dict) -> tuple[dict[int, list[dict]], bool]:
    if not isinstance(middle, dict) or (
        middle.get("schema") != "docvortex.middle"
        or middle.get("schema_version") != "2.0"
        or middle.get("is_full_document") is not True
    ):
        return {}, False
    metadata, pages = middle.get("metadata"), middle.get("pages")
    if not isinstance(metadata, dict) or metadata.get("producer") != producer or not isinstance(pages, list):
        return {}, False
    indexed: dict[int, list[dict]] = {}
    for page in pages:
        if not isinstance(page, dict):
            return {}, False
        index, blocks = page.get("page_idx"), page.get("blocks")
        if (type(index) is not int or index < 0 or index in indexed or not isinstance(blocks, list)
                or not all(isinstance(block, dict) for block in blocks)):
            return {}, False
        indexed[index] = blocks
    extensions = middle.get("extensions")
    mineru = extensions.get("mineru") if isinstance(extensions, dict) else None
    return indexed, isinstance(mineru, dict) and mineru.get("parse_mode") == "ocr"


def _normalized_bbox(value: object) -> tuple[float, float, float, float] | None:
    if not isinstance(value, list) or len(value) != 4 or not all(
        type(item) in (int, float) and 0 <= item <= 1 and math.isfinite(item) for item in value
    ):
        return None
    left, top, right, bottom = value
    return (left, top, right, bottom) if left < right and top < bottom else None


def _pdf_captions(value: object) -> list[dict]:
    if not isinstance(value, list):
        return []
    return [
        {"content": item["content"], "bbox": _normalized_bbox(item.get("bbox"))}
        for item in value
        if isinstance(item, dict) and isinstance(item.get("content"), str) and item["content"].strip()
    ]


class _WordImageOCR:
    def __init__(
        self, archive: zipfile.ZipFile, read_budget: int, text_budget: int,
        recognize: Callable[[bytes, str], list[ParsedBlock]],
    ) -> None:
        self.archive = archive
        self.read_budget = read_budget
        self.text_budget = text_budget
        self.recognize = recognize
        self.count = 0
        self.warnings: set[str] = set()

    def read(self, source: str) -> list[ParsedBlock]:
        if not _OFFICE_IMAGE_PATH.fullmatch(source) or source not in self.archive.NameToInfo:
            self.warnings.add("word_image_ocr_asset_unavailable")
            return []
        entry = self.archive.getinfo(source)
        if self.count >= _MAX_OFFICE_IMAGES or entry.file_size > self.read_budget:
            self.warnings.add("word_image_ocr_budget_exceeded")
            return []
        self.count += 1
        self.read_budget -= entry.file_size
        payload = self.archive.read(entry)
        media = detect_media_type(payload, "")
        if not media.startswith("image/"):
            self.warnings.add("word_image_ocr_format_unsupported")
            return []
        blocks = self.recognize(payload, media)
        if not blocks:
            self.warnings.add("word_image_ocr_no_text")
        self.text_budget -= sum(len(block.text.encode("utf-8")) for block in blocks)
        if self.text_budget < 0:
            raise ValueError("OCR 输出超限")
        return blocks


def normalize_archive(
    data: bytes, media: str, limit: int, *,
    recognize_image: Callable[[bytes, str], list[ParsedBlock]] | None = None,
    image_budget: int = 0,
    allow_empty: bool = False,
) -> list[ParsedBlock]:
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        entries = archive.infolist()
        if len(entries) > 2048:
            raise ValueError("解压预算超限")
        names = [e.filename for e in entries]
        if len(names) != len(set(names)) or names.count("structured_content.json") != 1:
            raise ValueError("结构结果缺失或重复")
        # 仅读取协议规定的 JSON，实际读取的成员共享解压预算。
        content_entry = next(e for e in entries if e.filename == "structured_content.json")
        if content_entry.file_size > limit:
            raise ValueError("解压预算超限")
        content = json.loads(archive.read(content_entry))
        middle = None
        if media == "application/pdf" and "middle_json.json" in names:
            middle_entry = archive.getinfo("middle_json.json")
            if middle_entry.file_size <= limit - content_entry.file_size:
                try:
                    middle = json.loads(archive.read(middle_entry))
                except (ValueError, zipfile.BadZipFile):
                    # 附加坐标损坏时保留已验证的正文，布局策略会标记缺失。
                    pass
        word_ocr = (
            _WordImageOCR(archive, image_budget, limit - content_entry.file_size, recognize_image)
            if media in {DOC, DOCX} and recognize_image is not None else None
        )
        return _normalize_content(content, middle, media, word_ocr, allow_empty=allow_empty)


def _normalize_content(
    content: dict, middle: object, media: str, word_ocr: _WordImageOCR | None, *, allow_empty: bool,
) -> list[ParsedBlock]:
    if content.get("is_full_document") is not True or not isinstance(content.get("pages"), list):
        raise ValueError("不接受截断文档")
    producer = content.get("metadata", {}).get("producer", {})
    if producer.get("name") != "mineru" or not str(producer.get("version", "")).startswith("4.1."):
        raise ValueError("解析协议版本不匹配")
    geometry, ocr = _pdf_boxes(middle, producer)
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
        # 同一包的两个输出按协议保留顶层块数量、类型和顺序。逐页校验后才能配对。
        middle_blocks = geometry.get(index, [])
        aligned = len(middle_blocks) == len(page["blocks"]) and all(
            block.get("type") == middle_block.get("type")
            for block, middle_block in zip(page["blocks"], middle_blocks, strict=True)
        )
        for offset, block in enumerate(page["blocks"]):
            value = block.get("content")
            warnings = {"mineru_requires_review", "mineru_geometry_unavailable"}
            source_kind = str(block.get("type", "text")).lower()
            kind = "table" if "table" in source_kind else {
                "title": "heading", "heading": "heading", "caption": "caption",
                "doc_title": "heading", "paragraph_title": "heading",
                "footnote": "footnote", "image": "figure", "figure": "figure",
                "header": "header", "page_header": "header", "footer": "footer",
                "page_footer": "footer", "page_number": "footer",
            }.get(source_kind, "text")
            images: list[str] = []
            if word_ocr is not None:
                source = block.get("image_source")
                if isinstance(source, str) or kind == "figure":
                    images.append(source if isinstance(source, str) else "")
                if isinstance(value, str) and "<img" in value.lower():
                    markup = HtmlText()
                    markup.feed(value)
                    images.extend(markup.images)
                for image_source in dict.fromkeys(images):
                    for recognized in word_ocr.read(image_source):
                        result.append(ParsedBlock(
                            recognized.text,
                            Locator(EXTENSIONS[media][1:], 0, len(recognized.text), "block", block=len(result)),
                            tuple(sorted({
                                *recognized.warnings, "word_image_ocr_requires_review",
                                "mineru_native_location_unavailable",
                            })),
                            kind="figure",
                        ))
            metadata = {}
            if media == "application/pdf":
                pdf_kind = {
                    "doc_title": "heading", "paragraph_title": "heading", "equation": "equation",
                    "chart": "figure", "code": "code", "page_footnote": "footnote", "aside_text": "aside",
                }.get(source_kind, kind)
                box = _normalized_bbox(middle_blocks[offset].get("bbox")) if aligned else None
                metadata = {
                    "pdf_kind": pdf_kind, "pdf_ocr": ocr, "pdf_bbox": box,
                    "pdf_continues_prev": block.get("continues_prev") is True,
                    "pdf_captions": _pdf_captions(block.get("captions")),
                }
            if (not isinstance(value, str) or not value.strip()) and images and kind == "figure":
                continue
            if not isinstance(value, str) or not value.strip():
                skipped = True
                # 空的视觉块仍可作为通栏边界，旧策略会略过它。
                if metadata.get("pdf_kind") not in {"figure", "table", "equation"} or not (
                    metadata.get("pdf_bbox") or metadata.get("pdf_captions")
                ):
                    continue
                value = ""
            if (images and "<img" in value.lower()) or "table" in source_kind or re.search(
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
            if not value.strip() and not metadata.get("pdf_bbox") and not metadata.get("pdf_captions"):
                skipped = True
                continue
            if "\x00" in value:
                # 等长替换，保留块内码点偏移；原件不变，不能静默丢弃异常字符。
                value = value.replace("\x00", "\ufffd")
                warnings.add("mineru_nul_replaced")
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
                    kind=kind,
                    heading_level=block.get("level") if type(block.get("level")) is int
                    and 1 <= block["level"] <= 6 else None,
                    table_id=f"mineru-{index}-{len(result)}" if kind == "table" else None,
                    metadata=metadata,
                )
            )
    if not allow_empty and not any(block.text.strip() or block.metadata.get("pdf_captions") for block in result):
        raise ValueError("没有可用正文")
    if result and (skipped or (word_ocr is not None and word_ocr.warnings)):
        index = next((index for index, block in enumerate(result) if block.text.strip()), 0)
        first = result[index]
        warnings = {*first.warnings, *(word_ocr.warnings if word_ocr else ())}
        if skipped:
            warnings.add("mineru_content_skipped")
        result[index] = replace(first, warnings=tuple(sorted(warnings)))
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
        self.limit = settings.mineru_output_bytes
        self.json_limit = settings.parse_output_bytes

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type in EXTENSIONS

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        if not self.supports(media_type, filename):
            raise ParseFailed("MinerU不支持该文件类型")
        return self._parse(data, media_type, time.monotonic() + self.timeout)

    def _parse(
        self, data: bytes, media_type: str, deadline: float, *, embedded_image: bool = False,
    ) -> list[ParsedBlock]:
        request_id = uuid.uuid4().hex
        boundary = "ks" + uuid.uuid4().hex
        fields = {
            "tier": "basic",
            "ocr_mode": "ocr" if embedded_image else "auto",
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

        def recognize_image(payload: bytes, media: str) -> list[ParsedBlock]:
            return self._parse(payload, media, deadline, embedded_image=True)

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
                        return normalize_archive(
                            raw, media_type, self.json_limit,
                            recognize_image=recognize_image if media_type in {DOC, DOCX} else None,
                            image_budget=self.limit, allow_empty=embedded_image,
                        )
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
        except ParseFailed:
            raise
        except Exception as exc:
            raise ParseFailed("MinerU不可用或解析结果无效") from exc
