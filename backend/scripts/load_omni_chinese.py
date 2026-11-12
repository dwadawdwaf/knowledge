"""下载 OmniDocBench 中文页并批量导入，逐文件记录“上传到入库”的耗时。

数据来自 Hugging Face 镜像（官方数据集 opendatalab/OmniDocBench）：
标注 `OmniDocBench.json` 与页面图片都在缓存目录本地留存，
每 `--pages-per-file` 页按原始像素打包成一个只含扫描页的 PDF，走真实 MinerU 解析。

执行分三段：页面图并行预取 → 全部文件并行提交上传 → 轮询各任务到终态。

计时口径（每个文件）：
- upload_seconds：发出上传请求 -> 收到 202 受理
- preparation_seconds：读取已缓存页面、转换和打包 PDF 的时间
- queue_observed_seconds：受理 -> 首次观察到 running，仅在观察到 queued 后才记录
- processing_observed_seconds：观察到 running -> 首次观察到终态；重试时不估算
- observed_since_ack_seconds：受理 -> 首次观察到终态，包含可能的观察延迟
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import tempfile
import threading
import time
import zlib
from concurrent.futures import ThreadPoolExecutor, as_completed
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

# 限制扫描页像素，避免畸形或超大图片在多线程解码时耗尽内存。
MAX_PAGE_PIXELS = 40_000_000
Image.MAX_IMAGE_PIXELS = MAX_PAGE_PIXELS

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
SUBMIT_WORKERS = 4
POLL_WORKERS = 6
_LOCK = threading.Lock()


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


def check_image_size(source: Image.Image) -> None:
    width, height = source.size
    if width <= 0 or height <= 0 or width * height > MAX_PAGE_PIXELS:
        raise RuntimeError("页面图片像素超过导入上限，请检查原图")


def image(net: httpx.Client, name: str) -> bytes:
    """取页面图片：先本地缓存，再走镜像；只有能被 PIL 解码才算成功。"""
    if not name or name in {".", ".."} or any(char in name for char in ("/", "\\", ":")):
        raise RuntimeError("页面图片名称不合法")
    cached = IMAGES / name
    if cached.is_file():
        raw = cached.read_bytes()
        try:
            with Image.open(io.BytesIO(raw)) as source:
                check_image_size(source)
                return raw
        except OSError as exc:
            raise RuntimeError("页面缓存无法解码，请检查原缓存后重试") from exc
    url = f"{MIRROR}/{quote('images/' + name, safe='/')}"
    for attempt in range(4):
        try:
            response = net.get(url)
            response.raise_for_status()
            raw = response.content
            with Image.open(io.BytesIO(raw)) as source:
                check_image_size(source)
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
        check_image_size(source)
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


def library(client: httpx.Client, title: str) -> str:
    for item in expect(client.get("/api/v1/knowledge-bases"), 200)["items"]:
        if item["name"] == title:
            return item["id"]
    return expect(client.post("/api/v1/knowledge-bases", json={"name": title}), 201)["id"]


def chunks(client: httpx.Client, document: str) -> int:
    data = expect(client.get(f"/api/v1/documents/{document}/chunks?limit=1"), 200)
    return data["total"]


def verify_cached_record(client: httpx.Client, kb: str, record: dict[str, Any]) -> None:
    """仅复用当前知识库中仍可验证的资料和任务，避免跨库误跳过。"""
    document_id = record.get("document_id")
    if not document_id:
        raise RuntimeError("缓存记录缺少资料 ID，请使用独立缓存目录后重新导入")
    response = client.get(f"/api/v1/documents/{document_id}")
    if response.status_code == 404:
        raise RuntimeError("缓存资料在当前库不存在，请使用独立缓存目录后重新导入")
    document = expect(response, 200)
    if document.get("kb_id") != kb:
        raise RuntimeError("缓存资料不属于当前知识库，请使用独立缓存目录后重新导入")
    if record.get("state") == "succeeded" and not document.get("active_build_id"):
        raise RuntimeError("缓存资料已无发布构建，请检查资料状态后恢复")
    if not record.get("job_id") or (document.get("job") or {}).get("id") != record["job_id"]:
        raise RuntimeError("缓存任务不是资料的当前任务，请检查资料状态后恢复")


def save(records: list[dict[str, Any]]) -> None:
    temporary = TIMING.with_suffix(".tmp")
    temporary.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, TIMING)


def update(records: list[dict[str, Any]], record: dict[str, Any]) -> None:
    """线程安全地把一条记录写回列表并落盘。"""
    with _LOCK:
        records[:] = [row for row in records if row["file"] != record["file"]] + [record]
        save(records)


def submit_one(
    net: httpx.Client,
    client: httpx.Client,
    kb: str,
    filename: str,
    batch: list[str],
    start_page: int,
    end_page: int,
    existing: dict[str, Any] | None,
) -> dict[str, Any]:
    """打包并上传一个文件；已有 submitted 记录时复用其任务号，不重复上传。"""
    if existing and existing.get("job_id") and existing.get("state") == "submitted":
        return {**existing, "state": "submitted"}
    preparation_started = time.monotonic()
    assets = [raster(image(net, item)) for item in batch]
    content = pack(assets)
    preparation_seconds = time.monotonic() - preparation_started
    started_at = now()
    started = time.monotonic()
    response = client.post(
        f"/api/v1/knowledge-bases/{kb}/documents",
        data={"parser_profile": "mineru-api4.1-v1"},
        files={"file": (filename, content, "application/pdf")},
        timeout=180,
    )
    ack = expect(response, 200 if response.status_code == 200 else 202)
    acknowledged = time.monotonic()
    return {
        "file": filename,
        "pages": len(batch),
        "start_page": start_page,
        "end_page": end_page,
        "bytes": len(content),
        "upload_started_at": started_at,
        "upload_acknowledged_at": now(),
        "preparation_seconds": round(preparation_seconds, 2),
        "upload_seconds": round(acknowledged - started, 2),
        "document_id": ack["document_id"],
        "job_id": ack["job_id"],
        "job_created_at": None,
        "state": "submitted",
        "error": None,
    }


def poll_all(client: httpx.Client, pending: list[dict[str, Any]], timeout: float):
    """每轮观察全部任务；无法确认的时间保留为空，不把完成时间伪装成排队时间。"""
    last_change = time.monotonic()
    trackers: dict[str, dict[str, Any]] = {
        row["job_id"]: {"stages": [], "saw_queued": False, "running_at": None, "retried": False}
        for row in pending
    }
    with ThreadPoolExecutor(max_workers=POLL_WORKERS) as pool:
        while pending:
            if time.monotonic() - last_change >= timeout:
                raise RuntimeError(f"任务状态持续无变化（{timeout:.0f}s）")
            futures = {
                pool.submit(client.get, f"/api/v1/jobs/{row['job_id']}"): row for row in pending
            }
            for future in as_completed(futures):
                row = futures[future]
                data = expect(future.result(), 200)
                tracker = trackers[row["job_id"]]
                observed_at = datetime.now(UTC)
                if data.get("attempt", 0) > 1:
                    tracker["retried"] = True
                stage = data.get("stage") or data["state"]
                if not tracker["stages"] or tracker["stages"][-1] != stage:
                    tracker["stages"].append(stage)
                    last_change = time.monotonic()
                if data["state"] == "queued":
                    if tracker["running_at"] is not None:
                        tracker["retried"] = True
                    tracker["saw_queued"] = True
                elif data["state"] == "running" and tracker["running_at"] is None:
                    tracker["running_at"] = observed_at
                if data["state"] not in TERMINAL:
                    continue
                acknowledged_at = row.get("upload_acknowledged_at")
                acknowledged = datetime.fromisoformat(acknowledged_at) if acknowledged_at else None
                running_at = tracker["running_at"]
                valid_split = tracker["saw_queued"] and running_at is not None and not tracker["retried"]
                result = {
                    **row,
                    "job_created_at": data.get("created_at"),
                    "stages": tracker["stages"],
                    "state": data["state"],
                    "error": (data.get("last_error") or {}).get("code"),
                    "terminal_observed_at": observed_at.isoformat(timespec="milliseconds"),
                    "queue_observed_seconds": round((running_at - acknowledged).total_seconds(), 2)
                    if valid_split and acknowledged else None,
                    "processing_observed_seconds": round((observed_at - running_at).total_seconds(), 2)
                    if valid_split else None,
                    "observed_since_ack_seconds": round((observed_at - acknowledged).total_seconds(), 2)
                    if acknowledged else None,
                }
                for obsolete in ("ack_monotonic", "queue_seconds", "processing_seconds", "ingest_seconds"):
                    result.pop(obsolete, None)
                if data["state"] == "succeeded":
                    result["chunks"] = chunks(client, row["document_id"])
                pending.remove(row)
                yield result
            if pending:
                time.sleep(2)


def summary(records: list[dict[str, Any]]) -> None:
    done = [row for row in records if row["state"] == "succeeded"]
    say("")
    say(
        f"{'文件':<36}{'页':>4}{'上传s':>8}{'排队约s':>9}{'处理约s':>9}{'观察总s':>9}"
        f"{'分块':>6}  状态"
    )
    for row in sorted(records, key=lambda item: item["file"]):
        say(
            f"{row['file']:<36}{row['pages']:>4}{row['upload_seconds']:>8.2f}"
            f"{format_seconds(row.get('queue_observed_seconds')):>9}"
            f"{format_seconds(row.get('processing_observed_seconds')):>9}"
            f"{format_seconds(row.get('observed_since_ack_seconds')):>9}"
            f"{row.get('chunks', 0):>6}  {row['state']}"
        )
    if done:
        pages = sum(row["pages"] for row in done)
        say("")
        say(f"成功 {len(done)}/{len(records)} 个文件，{pages} 页；空值表示无法可靠推算。")
    say(f"明细：{TIMING}")


def format_seconds(value: float | None) -> str:
    return "—" if value is None else f"{value:.1f}"


def completed_stage_times(log_file: Path, job_ids: set[str]) -> dict[str, dict[str, float]]:
    """汇总当前 Worker 已记账阶段；失败中断的阶段可能不完整。"""
    totals: dict[str, dict[str, float]] = {}
    if not log_file.is_file():
        return totals
    with log_file.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict) or event.get("event") != "stage_done":
                continue
            job_id, stage, milliseconds = event.get("job_id"), event.get("stage"), event.get("duration_ms")
            if (job_id not in job_ids or stage not in {"parsing", "chunking", "enhancing", "embedding", "publishing"}
                    or not isinstance(milliseconds, (int, float)) or isinstance(milliseconds, bool)):
                continue
            stages = totals.setdefault(job_id, {})
            stages[stage] = round(stages.get(stage, 0.0) + milliseconds / 1000, 3)
    return totals


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, help="只导入前 N 个文件，便于先跑小批量")
    parser.add_argument("--pages-per-file", type=int, default=12, help="每个 PDF 打包的页数")
    parser.add_argument("--port", type=int, default=8011, help="工作台 API 端口")
    parser.add_argument("--job-timeout", type=float, default=14400, help="全部任务持续无阶段变化的上限秒数")
    parser.add_argument(
        "--mineru-timeout",
        type=float,
        default=1500,
        help="等待 MinerU 解析的上限秒数；CPU 上中文书页约 40-60 s/页",
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

        selected = []
        for index, batch in enumerate(batches, 1):
            start_page = (index - 1) * args.pages_per_file + 1
            end_page = min(index * args.pages_per_file, len(pages))
            filename = f"OmniDocBench 中文页 {start_page:03d}-{end_page:03d}.pdf"
            existing = next((row for row in records if row["file"] == filename), None)
            selected.append((filename, batch, start_page, end_page, existing))
        cached = [item[4] for item in selected if item[4] is not None]
        if cached:
            say(f"[续跑] 核对 {len(cached)} 条缓存资料与当前知识库…")
            with ThreadPoolExecutor(max_workers=POLL_WORKERS) as pool:
                list(pool.map(lambda row: verify_cached_record(client, kb, row), cached))

        todo = []
        for filename, batch, start_page, end_page, existing in selected:
            if existing and existing.get("state") == "succeeded":
                say(f"[跳过] {filename} 已入库")
                continue
            if existing and existing.get("state") in {"failed", "cancelled"}:
                say(f"[跳过] {filename} 已终止，请先检查任务并按运行手册恢复")
                continue
            todo.append((filename, batch, start_page, end_page, existing))

        # 只预取本轮需要打包的页面；续跑已提交任务不重新读取图片。
        needed = [
            page for _, batch, _, _, existing in todo
            if not existing or existing.get("state") != "submitted" or not existing.get("job_id")
            for page in batch
        ]
        say(f"[图片] 预取 {len(needed)} 张页面图…")
        prefetch_started = time.monotonic()
        with ThreadPoolExecutor(max_workers=6) as pool:
            for fetched, _ in enumerate(pool.map(lambda item: image(net, item), needed), 1):
                if fetched % 60 == 0 or fetched == len(needed):
                    say(f"[图片] {fetched}/{len(needed)}")
        say(f"[图片] 本轮预取耗时 {time.monotonic() - prefetch_started:.2f}s")

        # Worker 仍按自己的并发数处理，上传并发只影响入队速度。
        say(f"[提交] 并行上传 {len(todo)} 个文件…")
        pending = []
        with ThreadPoolExecutor(max_workers=SUBMIT_WORKERS) as pool:
            futures = [
                pool.submit(submit_one, net, client, kb, *item)
                for item in todo
            ]
            for index, future in enumerate(as_completed(futures), 1):
                record = future.result()
                update(records, record)
                pending.append(record)
                say(f"[提交 {index}/{len(todo)}] {record['file']} job={record['job_id']}")

        # 第三段：并行轮询全部任务到终态。
        say(f"[轮询] {len(pending)} 个任务…")
        pending_count = len(pending)
        current_jobs = {row["job_id"] for row in pending}
        for finished_count, record in enumerate(poll_all(client, pending, args.job_timeout), 1):
            update(records, record)
            say(
                f"[{finished_count}/{pending_count}] {record['file']} {record['state']} "
                f"观察排队 {format_seconds(record.get('queue_observed_seconds'))}s / "
                f"观察处理 {format_seconds(record.get('processing_observed_seconds'))}s "
                f"/ 分块 {record.get('chunks', 0)}"
            )
        stage_times = completed_stage_times(runtime.log_dir / "worker.log", current_jobs)
        for row in records:
            if row.get("job_id") in stage_times:
                row["completed_stage_seconds"] = stage_times[row["job_id"]]
        if stage_times:
            save(records)
            say("[阶段] 已记录当前 Worker 日志中完成的阶段耗时；重试或中断时可能不完整。")

    summary(records)


if __name__ == "__main__":
    if isinstance(sys.stdout, io.TextIOWrapper):
        sys.stdout.reconfigure(encoding="utf-8")
    main()
