"""只调用固定版本的 DeepDoc，不调用视觉大模型或抓取外链。"""

from __future__ import annotations

import contextlib
import io
import logging
import os
from html.parser import HTMLParser

from knowledge.adapters.deepdoc import PROFILE
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed, PasswordProtected


class _TableText(HTMLParser):
    """保留行列分隔；不执行 HTML，不把 HTML 交给浏览器。"""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.hidden += 1
        elif tag in {"tr", "br"}:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"}:
            self.hidden = max(0, self.hidden - 1)
        elif tag in {"td", "th"}:
            self.parts.append("\t")
        elif tag == "tr":
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def normalize_boxes(boxes: list[dict], page_count: int) -> list[ParsedBlock]:
    """把上游块转为服务证据；多页位置完整保留，不伪造字符级精度。"""
    warnings = {"deepdoc_ocr_requires_review"}
    result: list[ParsedBlock] = []
    seen_pages: set[int] = set()
    for index, box in enumerate(boxes):
        kind = box.get("layout_type", "text")
        if kind == "figure":
            warnings.add("deepdoc_figures_not_described")
            continue
        text = box.get("text", "")
        if not isinstance(text, str):
            raise ParseFailed("DeepDoc 文本类型异常")
        if kind == "table":
            warnings.add("deepdoc_table_linearized")
            table = _TableText()
            table.feed(text)
            text = "\n".join(
                line.strip() for line in "".join(table.parts).splitlines() if line.strip()
            )
        if not text.strip():
            continue
        positions = tuple(tuple(p) for p in box.get("positions", []))
        if not positions or any(len(p) != 5 or not 1 <= p[0] <= page_count for p in positions):
            raise ParseFailed("DeepDoc 缺少有效定位")
        seen_pages.update(p[0] for p in positions)
        result.append(
            ParsedBlock(
                text=text,
                locator=Locator(
                    type="pdf",
                    char_start=0,
                    char_end=len(text),
                    precision="block",
                    page=positions[0][0],
                    block=index,
                    positions=positions,
                ),
            )
        )
    if len(seen_pages) < page_count:
        warnings.add("deepdoc_empty_page")
    return [ParsedBlock(block.text, block.locator, tuple(sorted(warnings))) for block in result]


class DeepDocRuntime:
    name = PROFILE

    def __init__(self, max_pages: int = 50, max_image_pixels: int = 40_000_000) -> None:
        self.max_pages = max_pages
        self.max_image_pixels = max_image_pixels

    def supports(self, media_type: str, filename: str) -> bool:
        return media_type in {"application/pdf", "image/png", "image/jpeg", "image/tiff"}

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        # 上游调试日志可能含 OCR 文本；整个子进程中禁用并丢弃第三方标准输出。
        logging.disable(logging.CRITICAL)
        with (
            open(os.devnull, "w") as sink,
            contextlib.redirect_stdout(sink),
            contextlib.redirect_stderr(sink),
        ):
            return self._parse(data, media_type)

    def _parse(self, data: bytes, media_type: str) -> list[ParsedBlock]:
        from pypdf import PdfReader

        if media_type != "application/pdf":
            from PIL import Image, ImageSequence

            Image.MAX_IMAGE_PIXELS = self.max_image_pixels
            with Image.open(io.BytesIO(data)) as image:
                if getattr(image, "n_frames", 1) > self.max_pages:
                    raise ParseFailed("图片页数超限")
                frames = []
                for frame in ImageSequence.Iterator(image):
                    if frame.width * frame.height > self.max_image_pixels:
                        raise ParseFailed("图片像素超限")
                    frames.append(frame.convert("RGB"))
                stream = io.BytesIO()
                frames[0].save(
                    stream, format="PDF", save_all=True, append_images=frames[1:], resolution=72
                )
                data = stream.getvalue()
        reader = PdfReader(io.BytesIO(data))
        if reader.is_encrypted:
            raise PasswordProtected("文件已加密")
        page_count = len(reader.pages)
        if not 1 <= page_count <= self.max_pages:
            raise ParseFailed("PDF 页数超限")
        from deepdoc.parser.pdf_parser import RAGFlowPdfParser

        # 不使用 rag.app.naive 的图片描述、超链接递归和数据库绑定。
        boxes = RAGFlowPdfParser().parse_into_bboxes(data)
        return normalize_boxes(boxes, page_count)
