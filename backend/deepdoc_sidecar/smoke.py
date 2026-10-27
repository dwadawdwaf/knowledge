"""在真实边车容器内生成并识别自制样本；不读取用户文件或调用生成模型。"""

from __future__ import annotations

import io

from PIL import Image, ImageDraw, ImageFont

from deepdoc_sidecar.service import SidecarSettings
from knowledge.adapters.deepdoc import DeepDocParser
from knowledge.config import Settings


def main() -> int:
    settings = SidecarSettings()
    parser = DeepDocParser(Settings(deepdoc_token=settings.token))
    image = Image.new("RGB", (1200, 900), "white")
    draw = ImageDraw.Draw(image)
    font = ImageFont.load_default(size=32)
    draw.text((80, 60), "SELF AUTHORED ROOM POLICY", fill="black", font=font)
    draw.text((80, 140), "Close air conditioner after use.", fill="black", font=font)
    for y in (280, 380, 480, 580):
        draw.line((80, y, 1080, y), fill="black", width=3)
    for x in (80, 580, 1080):
        draw.line((x, 280, x, 580), fill="black", width=3)
    for text, x, y in (
        ("Department", 100, 310),
        ("Quota", 600, 310),
        ("Admin", 100, 410),
        ("100", 600, 410),
        ("Research", 100, 510),
        ("200", 600, 510),
    ):
        draw.text((x, y), text, fill="black", font=font)
    failed = False
    samples = [
        ("PNG", "image/png"),
        ("JPEG", "image/jpeg"),
        ("TIFF", "image/tiff"),
        ("PDF", "application/pdf"),
    ]
    for index, (format_name, media) in enumerate(samples, 1):
        print(f"[{index}/4] Real DeepDoc: self-authored {format_name}", flush=True)
        stream = io.BytesIO()
        options = {"save_all": True, "append_images": [image]} if format_name == "TIFF" else {}
        image.save(stream, format=format_name, **options)
        try:
            blocks = parser.parse(stream.getvalue(), media, "self-authored")
            text = " ".join(block.text for block in blocks).casefold()
            text_ok = "air conditioner" in text
            table_ok = "admin" in text and "100" in text
            pages_ok = format_name != "TIFF" or {b.locator.page for b in blocks} == {1, 2}
            structure = any("deepdoc_table_linearized" in b.warnings for b in blocks)
            passed = text_ok and table_ok and pages_ok and structure
            failed |= not passed
            print(
                f"  blocks={len(blocks)} text={text_ok} table={table_ok} "
                f"pages={pages_ok} structure={structure} passed={passed}",
                flush=True,
            )
        except Exception:  # noqa: BLE001 - 自检不打印模型内部异常或资料正文。
            failed = True
            print("  FAILED: inspect service state; no fallback performed", flush=True)
    print(
        "Parsing smoke only; manual complex-layout and full citation acceptance still required.",
        flush=True,
    )
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
