"""导入计时与轮询使用自造任务，不读取语料或调用真实服务。"""

from __future__ import annotations

import io
import json
import threading
from datetime import UTC, datetime

import httpx
import pytest
from PIL import Image

from scripts import load_omni_chinese as loader


def test_oversized_image_is_rejected_before_rasterizing(monkeypatch):
    output = io.BytesIO()
    Image.new("RGB", (3, 2)).save(output, format="PNG")
    monkeypatch.setattr(loader, "MAX_PAGE_PIXELS", 5)
    with pytest.raises(RuntimeError, match="像素超过"):
        loader.raster(output.getvalue())


def test_corrupt_cached_image_is_not_deleted(monkeypatch, tmp_path):
    cached = tmp_path / "page.png"
    cached.write_bytes(b"not an image")
    monkeypatch.setattr(loader, "IMAGES", tmp_path)
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client,
        pytest.raises(RuntimeError, match="缓存无法解码"),
    ):
        loader.image(client, "page.png")
    assert cached.read_bytes() == b"not an image"


@pytest.mark.parametrize("name", ["../escape.png", "..\\escape.png", "C:\\escape.png", "..", ""])
def test_image_rejects_paths_outside_cache(name):
    with (
        httpx.Client(transport=httpx.MockTransport(lambda _: httpx.Response(500))) as client,
        pytest.raises(RuntimeError, match="名称不合法"),
    ):
        loader.image(client, name)


@pytest.mark.parametrize(
    ("status", "document", "state", "ok"),
    [
        (200, {"kb_id": "kb", "active_build_id": "build", "job": {"id": "job"}}, "succeeded", True),
        (200, {"kb_id": "other", "active_build_id": "build"}, "succeeded", False),
        (404, {}, "succeeded", False),
        (200, {"kb_id": "kb", "active_build_id": None}, "succeeded", False),
        (200, {"kb_id": "kb", "active_build_id": "build", "job": {"id": "other"}}, "succeeded", False),
        (200, {"kb_id": "kb", "job": {"id": "job"}}, "submitted", True),
        (200, {"kb_id": "kb", "job": {"id": "different"}}, "submitted", False),
    ],
)
def test_cached_records_must_match_current_knowledge_base_and_job(status, document, state, ok):
    def handle(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/api/v1/documents/doc"
        return httpx.Response(status, json={"data": document, "meta": {"request_id": "fixture"}})

    record = {"document_id": "doc", "job_id": "job", "state": state}
    with httpx.Client(base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)) as client:
        if ok:
            loader.verify_cached_record(client, "kb", record)
        else:
            with pytest.raises(RuntimeError):
                loader.verify_cached_record(client, "kb", record)


def test_poll_all_observes_every_job_and_does_not_invent_missing_durations(monkeypatch):
    counts: dict[str, int] = {}
    first_round: set[str] = set()
    lock = threading.Lock()
    jobs = [str(i) for i in range(8)]

    def handle(request: httpx.Request) -> httpx.Response:
        job = request.url.path.rsplit("/", 1)[-1]
        with lock:
            count = counts.get(job, 0)
            counts[job] = count + 1
            if count == 0:
                first_round.add(job)
            else:
                assert first_round == set(jobs)
        if job == "0":
            state, attempt = "failed", 1
        elif job == "1":
            state, attempt = ("queued", 1) if count == 0 else ("running", 2) if count == 1 else ("failed", 2)
        else:
            state, attempt = ("queued", 1) if count == 0 else ("running", 1) if count == 1 else ("failed", 1)
        return httpx.Response(200, json={"data": {
            "state": state, "stage": state, "attempt": attempt, "last_error": None,
        }, "meta": {"request_id": "fixture"}})

    monkeypatch.setattr(loader.time, "sleep", lambda _: None)
    ack = datetime.now(UTC).isoformat()
    pending = [{
        "job_id": job, "file": job, "upload_acknowledged_at": ack,
        "queue_seconds": 999, "processing_seconds": 999, "ack_monotonic": 123,
    } for job in jobs]
    with httpx.Client(base_url="http://fixture.invalid", transport=httpx.MockTransport(handle)) as client:
        results = {row["job_id"]: row for row in loader.poll_all(client, pending, timeout=10)}

    assert not pending and len(results) == 8
    assert results["0"]["queue_observed_seconds"] is None
    assert results["0"]["processing_observed_seconds"] is None
    assert results["1"]["queue_observed_seconds"] is None
    assert results["1"]["processing_observed_seconds"] is None
    assert results["2"]["queue_observed_seconds"] is not None
    assert results["2"]["processing_observed_seconds"] is not None
    assert "queue_seconds" not in results["2"]
    assert "ack_monotonic" not in results["2"]


def test_completed_stage_times_uses_only_matching_jobs_and_sums_completed_attempts(tmp_path):
    logfile = tmp_path / "worker.log"
    events = [
        {"event": "stage_done", "job_id": "ours", "stage": "parsing", "duration_ms": 1000},
        {"event": "stage_done", "job_id": "ours", "stage": "parsing", "duration_ms": 250},
        {"event": "stage_done", "job_id": "ours", "stage": "embedding", "duration_ms": 400},
        {"event": "stage_done", "job_id": "other", "stage": "parsing", "duration_ms": 999},
    ]
    logfile.write_text("\n".join(json.dumps(event) for event in events) + "\n{partial", encoding="utf-8")
    assert loader.completed_stage_times(logfile, {"ours"}) == {
        "ours": {"parsing": 1.25, "embedding": 0.4},
    }
