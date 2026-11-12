"""下载 OmniDocBench 中文页并分批导入，逐文件记录“上传到入库”的耗时。

数据来自 Hugging Face 镜像（官方数据集 opendatalab/OmniDocBench）：
标注 `OmniDocBench.json` 与页面图片都在 `.data/omni-chinese/` 本地缓存，
每 `--pages-per-file` 页按原始像素打包成一个只含扫描页的 PDF，走真实 MinerU 解析。

计时口径（每个文件三条）：
- upload_seconds：发出上传请求 -> 收到 202 受理
- ingest_seconds：受理 -> 任务进入终态（succeeded / failed / cancelled）
- total_seconds：发出上传请求 -> 任务进入终态
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import time
import zlib
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import quote

import httpx
from PIL import Image, ImageChops
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject

from scripts.deps import ensure_database
from scripts.http_scenario import expect
from scripts.local_runtime import WORKBENCH_OBJECTS, LocalRuntime
from scripts.real_model import _RealSettings

BACKEND = Path(__file__).resolve().parents[1]
# 过程文件全部落在缓存目录：标注、页面图、打包的 PDF 与计时结果都不进工作区。
ROOT = Path(os.environ.get("KS_OMNI_CACHE") or Path(tempfile.gettempdir()) / "ks-omni-chinese")
ANNOTATION = ROOT / "OmniDocBench.json"
IMAGES = ROOT / "images"
TIMING = ROOT / "timing.json"
MIRROR = "https://hf-mirror.com/datasets/opendatalab/OmniDocBench/resolve/main"
ANNOTATION_BYTES = 42_208_096
LANGUAGES = {"simplified_chinese", "traditional_chinese", "en_ch_mixed"}
MAX_BYTES = 44 * 1024 * 1024
TERMINAL = {"succeeded", "failed", "cancelled"}


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


def say(message: str) -> None:
    print(message, file=sys.__stdout__ or sys.stdout, flush=True)


def annotation() -> list[dict[str, Any]]:
    """读取标注，缺失时从镜像下载；长度不符即视为半成品。"""
    if not ANNOTATION.is_file() or ANNOTATION.stat().st_size != ANNOTATION_BYTES:
        part = ANNOTATION.with_suffix(".part")
        with (
            httpx.Client(timeout=300, follow_redirects=True) as net,
            net.stream("GET", f"{MIRROR}/OmniDocBench.json") as response,
        ):
            response.raise_for_status()
            with part.open("wb") as output:
                for block in response.iter_bytes(1 << 20):
                    output.write(block)
        if part.stat().st_size != ANNOTATION_BYTES:
            raise RuntimeError("标注文件大小与官方不一致，已中止")
        os.replace(part, ANNOTATION)
        say(f"[标注] 已下载 {ANNOTATION_BYTES / 2**20:.0f} MB")
    return json.loads(ANNOTATION.read_bytes())


def chinese_pages(raw: list[dict[str, Any]]) -> list[str]:
    """中文页的图片名，按文件名排序以保证分批与续跑稳定。"""
    names = []
    for page in raw:
        info = page["page_info"]
        if info["page_attribute"]["language"] not in LANGUAGES:
            continue
        names.append(PurePosixPath(info["image_path"]).name)
    unique = sorted(set(names))
    if len(unique) != len(names):
        raise RuntimeError("标注中存在重复页面")
    return unique


def image(net: httpx.Client, name: str) -> bytes:
    """取页面图片：先本地缓存，再走镜像；只有能被 PIL 解码才算成功。"""
    cached = IMAGES / name
    if cached.is_file():
        raw = cached.read_bytes()
        try:
            with Image.open(io.BytesIO(raw)):
                return raw
        except OSError:
            cached.unlink()
    url = f"{MIRROR}/{quote('images/' + name, safe='/')}"
    for attempt in range(4):
        try:
            response = net.get(url)
            response.raise_for_status()
            raw = response.content
            with Image.open(io.BytesIO(raw)):
                pass
            cached.write_bytes(raw)
            return raw
        except (httpx.HTTPError, OSError):
            if attempt == 3:
                raise
            time.sleep(2**attempt)
    raise RuntimeError(f"图片下载失败：{name}")


def raster(raw: bytes) -> tuple[int, int, str, str, bytes]:
    """把页面图转成 PDF 可嵌入的原始像素流，保持分辨率不变。"""
    with Image.open(io.BytesIO(raw)) as source:
        width, height = source.size
        if source.format == "JPEG" and source.mode in ("L", "RGB"):
            return width, height, source.mode, "DCTDecode", raw
        if "A" in source.getbands() or "transparency" in source.info:
            rgba = source.convert("RGBA")
            background = Image.new("RGBA", rgba.size, "white")
            background.alpha_composite(rgba)
            rgb = background.convert("RGB")
        else:
            rgb = source.convert("RGB")
        # PDF 预测器：逐行水平差分，像素值无损。
        previous = Image.new("RGB", rgb.size)
        previous.paste(rgb.crop((0, 0, width - 1, height)), (1, 0))
        delta = ImageChops.subtract_modulo(rgb, previous).tobytes()
        return width, height, "RGB", "FlateDecode", zlib.compress(delta, level=3)


def name(text: str) -> NameObject:
    return NameObject(text)


def pack(assets: list[tuple[int, int, str, str, bytes]]) -> bytes:
    """把若干页装成单文件 PDF，每页一个整幅图像 XObject。"""
    writer = PdfWriter()
    for width, height, mode, filter_name, stream in assets:
        obj = DecodedStreamObject()
        obj.set_data(stream)
        obj.update(
            {
                name("/Type"): name("/XObject"),
                name("/Subtype"): name("/Image"),
                name("/Filter"): name(f"/{filter_name}"),
                name("/Width"): NumberObject(width),
                name("/Height"): NumberObject(height),
                name("/ColorSpace"): name("/DeviceGray" if mode == "L" else "/DeviceRGB"),
                name("/BitsPerComponent"): NumberObject(8),
            }
        )
        if filter_name == "FlateDecode":
            obj[name("/DecodeParms")] = DictionaryObject(
                {
                    name("/Predictor"): NumberObject(2),
                    name("/Colors"): NumberObject(3),
                    name("/Columns"): NumberObject(width),
                    name("/BitsPerComponent"): NumberObject(8),
                }
            )
        page_width, page_height = width * 72 / 200, height * 72 / 200
        page = writer.add_blank_page(width=page_width, height=page_height)
        draw = DecodedStreamObject()
        draw.set_data(
            f"q {page_width:.4f} 0 0 {page_height:.4f} 0 0 cm /Scan Do Q\n".encode("ascii")
        )
        page[name("/Resources")] = DictionaryObject(
            {name("/XObject"): DictionaryObject({name("/Scan"): writer._add_object(obj)})}
        )
        page[name("/Contents")] = writer._add_object(draw)
    out = io.BytesIO()
    writer.write(out)
    raw = out.getvalue()
    if len(raw) >= 50 * 2**20:
        raise RuntimeError("打包后的 PDF 超过上传上限")
    return raw


def wait_job(client: httpx.Client, job: str, timeout: float) -> tuple[dict[str, Any], list[str]]:
    """轮询任务终态，同时记录经历过的阶段顺序。"""
    deadline = time.monotonic() + timeout
    stages: list[str] = []
    while time.monotonic() < deadline:
        data = expect(client.get(f"/api/v1/jobs/{job}"), 200)
        stage = data.get("stage") or data["state"]
        if not stages or stages[-1] != stage:
            stages.append(stage)
        if data["state"] in TERMINAL:
            return data, stages
        time.sleep(1)
    raise RuntimeError(f"任务 {job} 等待超时（{timeout:.0f}s）")


def chunks(client: httpx.Client, document: str) -> int:
    data = expect(client.get(f"/api/v1/documents/{document}/chunks?limit=1"), 200)
    return data["total"]


def library(client: httpx.Client, title: str) -> str:
    for item in expect(client.get("/api/v1/knowledge-bases"), 200)["items"]:
        if item["name"] == title:
            return item["id"]
    return expect(client.post("/api/v1/knowledge-bases", json={"name": title}), 201)["id"]


def save(records: list[dict[str, Any]]) -> None:
    temporary = TIMING.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, TIMING)


def summary(records: list[dict[str, Any]]) -> None:
    done = [row for row in records if row["state"] == "succeeded"]
    say("")
    say(f"{'文件':<38}{'页':>4}{'上传s':>9}{'入库s':>9}{'总计s':>9}{'分块':>7}  状态")
    for row in records:
        say(
            f"{row['file']:<38}{row['pages']:>4}{row['upload_seconds']:>9.2f}"
            f"{row['ingest_seconds']:>9.2f}{row['total_seconds']:>9.2f}"
            f"{row.get('chunks', 0):>7}  {row['state']}"
        )
    if done:
        pages = sum(row["pages"] for row in done)
        ingest = sum(row["ingest_seconds"] for row in done)
        say("")
        say(
            f"成功 {len(done)}/{len(records)} 个文件，{pages} 页；入库合计 {ingest:.1f}s，"
            f"平均每页 {ingest / pages:.2f}s"
        )
    say(f"明细：{TIMING}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="只导入前 N 个文件，便于先跑小批量")
    parser.add_argument("--pages-per-file", type=int, default=12, help="每个 PDF 打包的页数")
    parser.add_argument("--port", type=int, default=8011, help="工作台 API 端口")
    parser.add_argument("--job-timeout", type=float, default=1800, help="单个文件等待入库的上限秒数")
    parser.add_argument(
        "--mineru-timeout",
        type=float,
        default=900,
        help="等待 MinerU 解析的上限秒数；CPU 上中文书页约 50 s/页",
    )
    parser.add_argument(
        "--database-url",
        help="显式库地址；用于同服务器的隔离临时库，此时对象卷也落在缓存目录",
    )
    args = parser.parse_args()
    if args.pages_per_file < 1:
        parser.error("--pages-per-file 必须为正数")

    ROOT.mkdir(parents=True, exist_ok=True)
    IMAGES.mkdir(parents=True, exist_ok=True)
    pages = chinese_pages(annotation())
    batches = [
        pages[start : start + args.pages_per_file]
        for start in range(0, len(pages), args.pages_per_file)
    ]
    if args.limit:
        batches = batches[: args.limit]
    say(f"[样本] 中文页 {len(pages)} 个，本次导入 {sum(map(len, batches))} 页 / {len(batches)} 个文件")

    settings = _RealSettings().model_copy(
        update={"mineru_timeout_seconds": args.mineru_timeout}
    )
    if args.database_url:
        # 隔离库自带匹配的临时对象卷，跑完可直接丢弃，不污染持久卷。
        dsn = args.database_url
        objects = str(ROOT / "objects")
        say("[库] 使用显式指定的隔离库，对象卷落在缓存目录")
    else:
        dsn = ensure_database(settings, progress=say)
        objects = str(WORKBENCH_OBJECTS)
    records: list[dict[str, Any]] = json.loads(TIMING.read_bytes()) if TIMING.is_file() else []
    finished = {(row["file"], row["pages"]) for row in records if row["state"] == "succeeded"}

    with (
        LocalRuntime(
            database_url=dsn,
            storage_root=objects,
            model_settings=settings,
            port=args.port,
        ) as runtime,
        runtime.client() as client,
        httpx.Client(timeout=120, follow_redirects=True) as net,
    ):
        title = f"OmniDocBench · 中文页（{len(pages)} 页）"
        kb = library(client, title)
        say(f"[知识库] {kb}")

        # 全量页并行预取到缓存，避免解析等待期间穿插串行下载拖长总时长。
        say(f"[图片] 预取 {len(pages)} 张页面图…")
        fetched = 0
        with ThreadPoolExecutor(max_workers=6) as pool:
            for _ in pool.map(lambda item: image(net, item), pages):
                fetched += 1
                if fetched % 60 == 0 or fetched == len(pages):
                    say(f"[图片] {fetched}/{len(pages)}")

        first = 1
        for batch in batches:
            start_page, end_page = first, first + len(batch) - 1
            first = end_page + 1
            filename = f"OmniDocBench 中文页 {start_page:03d}-{end_page:03d}.pdf"
            if (filename, len(batch)) in finished:
                say(f"[跳过] {filename} 已入库")
                continue
            assets = [raster(image(net, item)) for item in batch]
            content = pack(assets)
            started = time.monotonic()
            started_at = now()
            response = client.post(
                f"/api/v1/knowledge-bases/{kb}/documents",
                data={"parser_profile": "mineru-api4.1-v1"},
                files={"file": (filename, content, "application/pdf")},
                timeout=180,
            )
            ack = expect(response, 200 if response.status_code == 200 else 202)
            acknowledged = time.monotonic()
            acknowledged_at = now()
            job, stages = wait_job(client, ack["job_id"], args.job_timeout)
            ended = time.monotonic()
            record = {
                "file": filename,
                "pages": len(batch),
                "start_page": start_page,
                "end_page": end_page,
                "bytes": len(content),
                "upload_started_at": started_at,
                "upload_acknowledged_at": acknowledged_at,
                "ingest_finished_at": now(),
                "upload_seconds": round(acknowledged - started, 2),
                "ingest_seconds": round(ended - acknowledged, 2),
                "total_seconds": round(ended - started, 2),
                "document_id": ack["document_id"],
                "job_id": ack["job_id"],
                "job_created_at": job.get("created_at"),
                "stages": stages,
                "state": job["state"],
                "error": (job.get("last_error") or {}).get("code"),
            }
            if job["state"] == "succeeded":
                record["chunks"] = chunks(client, ack["document_id"])
            records = [row for row in records if row["file"] != filename] + [record]
            save(records)
            say(
                f"[{len(records)}] {filename} {job['state']} "
                f"上传 {record['upload_seconds']:.2f}s / 入库 {record['ingest_seconds']:.2f}s "
                f"/ 分块 {record.get('chunks', 0)}"
            )
            del content, assets

    summary(sorted(records, key=lambda row: row["file"]))


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    main()
