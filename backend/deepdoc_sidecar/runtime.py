"""只调用固定版本的 DeepDoc，不调用视觉大模型或抓取外链。"""

from __future__ import annotations

from dataclasses import replace

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


def _merge_short_blocks(blocks: list[ParsedBlock], threshold: int = 16) -> list[ParsedBlock]:
    """把过短的标题块并入紧随其后的块。

    版面模型会把“第二条”这类编号单独切成一块（实测 3 字），
    单独成段会变成检索噪声；并入下一段后仍保留前者的位置作为起点。
    """
    merged: list[ParsedBlock] = []
    pending: ParsedBlock | None = None
    for block in blocks:
        current = block
        if pending is not None:
            if (
                pending.locator.page == current.locator.page
                and "deepdoc_table_linearized" not in current.warnings
            ):
                current = ParsedBlock(
                    f"{pending.text} {current.text}",
                    replace(current.locator, block=pending.locator.block),
                    tuple(sorted({*pending.warnings, *current.warnings})),
                )
                pending = None
            else:
                merged.append(pending)
                pending = None
        if len(current.text.strip()) < threshold and "deepdoc_table_linearized" not in current.warnings:
            pending = current
            continue
        merged.append(current)
    if pending is not None:
        merged.append(pending)
    return merged


def normalize_boxes(boxes: list[dict], page_count: int) -> list[ParsedBlock]:
    """把上游块转为服务证据；多页位置完整保留，不伪造字符级精度。

    警告按块归属：表格线性化只标在表格块上，图形未描述标在所在页的块上。
    早先的实现跨块复用一个集合，导致纯文本块也带上表格警告，下游无法判断。
    """
    base_warnings = ("deepdoc_ocr_requires_review",)
    result: list[ParsedBlock] = []
    # 页级信号：图形块本身不产出文本，只能挂在同页的文本块上。
    page_signals: dict[int, set[str]] = {}
    # 无定位的图形无法归页，升级为整份文档的信号。
    unplaced: set[str] = set()
    seen_pages: set[int] = set()
    for index, box in enumerate(boxes):
        kind = box.get("layout_type", "text")
        positions = tuple(tuple(p) for p in box.get("positions", []))
        if kind == "figure":
            if positions:
                for position in positions:
                    page_signals.setdefault(position[0], set()).add(
                        "deepdoc_figures_not_described"
                    )
            else:
                unplaced.add("deepdoc_figures_not_described")
            continue
        text = box.get("text", "")
        if not isinstance(text, str):
            raise ParseFailed("DeepDoc 文本类型异常")
        own: set[str] = set()
        if kind == "table":
            own.add("deepdoc_table_linearized")
            table = _TableText()
            table.feed(text)
            text = "\n".join(
                line.strip() for line in "".join(table.parts).splitlines() if line.strip()
            )
        if not text.strip():
            continue
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
                warnings=tuple(sorted({*base_warnings, *own})),
            )
        )
    if not result:
        return result

    # 上游返回的是版面探测顺序（按检测批次），不是阅读顺序；
    # 实测同一页会出现“第二条”排在“第一条”之前。按页、上边距、左边距排序后恢复阅读顺序。
    # 局限：只处理单一阅读顺序，多栏排版会按行交错，需要分栏时应先按栏分组。
    result.sort(
        key=lambda block: (
            block.locator.page or 0,
            min(position[3] for position in block.locator.positions),
            min(position[1] for position in block.locator.positions),
        )
    )
    # 排序后重新编号，块序号与阅读顺序一致。
    result = [
        ParsedBlock(
            block.text,
            replace(block.locator, block=order),
            block.warnings,
        )
        for order, block in enumerate(result)
    ]

    # 没有文本块的页只能把信号记在首块，避免整页信号被静默丢弃。
    orphan: set[str] = set(unplaced)
    empty_pages = set(range(1, page_count + 1)) - seen_pages
    if empty_pages:
        orphan.add("deepdoc_empty_page")
    for page, signals in page_signals.items():
        if page not in seen_pages:
            orphan |= signals
    for index, block in enumerate(result):
        page = block.locator.page
        extra = set(page_signals.get(page, ()))
        if index == 0:
            extra |= orphan
        if extra:
            result[index] = ParsedBlock(
                block.text, block.locator, tuple(sorted({*block.warnings, *extra}))
            )
    return _merge_short_blocks(result)


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