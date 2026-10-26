"""演示与测试共用的纯 HTTP 验收流程，资料均为自制样本。"""

from __future__ import annotations

import time
from collections.abc import Callable

import httpx

from scripts.local_runtime import BACKEND, LocalRuntime

QUESTION = "会议室使用后需要做什么？"


def expect(response: httpx.Response, status: int) -> dict:
    if response.status_code != status:
        code = response.json().get("error", {}).get("code", "unknown")
        raise AssertionError(f"Expected HTTP {status}, got {response.status_code}, code={code}")
    payload = response.json()
    assert payload["meta"]["request_id"], "Missing request correlation ID"
    return payload.get("data", payload.get("error", {}))


def upload_sample(
    client: httpx.Client, kb_id: str, *, version: int = 1, document_id: str | None = None
) -> dict:
    path = BACKEND / "testdata" / "engineering" / f"e2e-room-v{version}.md"
    endpoint = (
        f"/api/v1/documents/{document_id}/versions"
        if document_id
        else f"/api/v1/knowledge-bases/{kb_id}/documents"
    )
    return expect(
        client.post(
            endpoint,
            files={
                "file": ("meeting-room.md", path.read_bytes(), "text/markdown"),
            },
        ),
        202,
    )


def wait_job(
    client: httpx.Client, job_id: str, *, progress: Callable[[str], None], timeout: float = 40
) -> dict:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        job = expect(client.get(f"/api/v1/jobs/{job_id}"), 200)
        progress(f"[job] state={job['state']} stage={job['stage']} attempt={job['attempt']}")
        if job["state"] == "succeeded":
            return job
        if job["state"] in ("failed", "cancelled"):
            code = (job.get("last_error") or {}).get("code")
            raise AssertionError(f"Job ended as {job['state']}, code={code}")
        time.sleep(0.5)
    raise AssertionError(f"Job did not finish within {timeout}s")


def run_lifecycle(runtime: LocalRuntime) -> dict:
    checks = {}
    with runtime.client() as client, runtime.client("demo-other-user") as other:
        runtime.say("[01/10] Create personal knowledge base via HTTP")
        kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "自制资料演示库"}), 201)
        runtime.say("[02/10] Upload multipart document; independent Worker processes it")
        uploaded = upload_sample(client, kb["id"])
        wait_job(client, uploaded["job_id"], progress=runtime.say)
        runtime.say("[03/10] Search: verify expected source and build")
        query = {"query": QUESTION, "knowledge_base_ids": [kb["id"]]}
        hits = expect(client.post("/api/v1/search", json=query), 200)["hits"]
        assert hits and "关闭照明" in hits[0]["excerpt"]
        assert all(hit["build_id"] == uploaded["build_id"] for hit in hits)
        runtime.say(f"[search] PASS: {len(hits)} hits, expected source present")
        runtime.say("[04/10] Answer: read actual HTTP response and citations")
        ask = {"question": QUESTION, "knowledge_base_ids": [kb["id"]]}
        answer = expect(client.post("/api/v1/qa", json=ask), 200)
        assert answer["answer_status"] == "answered" and "关闭照明" in answer["answer"]
        citation = answer["citations"][0]
        assert citation["build_id"] == uploaded["build_id"]
        runtime.say(f"[answer] PASS: status=answered, citations={len(answer['citations'])}")
        runtime.say("[05/10] Read citation source via HTTP and verify original text")
        chunk = expect(client.get(f"/api/v1/chunks/{citation['chunk_id']}"), 200)
        assert "关闭照明" in chunk["text"]
        assert chunk["version_id"] == uploaded["version_id"]
        runtime.say("[06/10] Unrelated question must return no_evidence")
        missing = expect(
            client.post(
                "/api/v1/qa",
                json={
                    "question": "量子纠缠星际导航协议是什么？",
                    "knowledge_base_ids": [kb["id"]],
                },
            ),
            200,
        )
        assert missing["answer_status"] == "no_evidence" and missing["citations"] == []
        runtime.say("[07/10] Other user's document/job/chunk/search access must return 404")
        for endpoint in (
            f"/api/v1/documents/{uploaded['document_id']}",
            f"/api/v1/jobs/{uploaded['job_id']}",
            f"/api/v1/chunks/{citation['chunk_id']}",
        ):
            expect(other.get(endpoint), 404)
        expect(other.post("/api/v1/search", json=query), 404)
        checks["cross_user"] = 404
        runtime.say("[08/10] Pause Worker, upload v2; published v1 must remain available")
        runtime.stop_worker()
        updated = upload_sample(client, kb["id"], version=2, document_id=uploaded["document_id"])
        pending = expect(client.get(f"/api/v1/jobs/{updated['job_id']}"), 200)
        assert pending["state"] == "queued"
        during = expect(client.post("/api/v1/search", json=query), 200)["hits"]
        assert during and all(hit["build_id"] == uploaded["build_id"] for hit in during)
        checks["old_version_while_pending"] = True
        runtime.say("[version] PASS: v2 queued, v1 still searchable")
        runtime.start_worker()
        wait_job(client, updated["job_id"], progress=runtime.say)
        runtime.say("[09/10] After publication, search/answer must use v2 only")
        after = expect(client.post("/api/v1/search", json=query), 200)["hits"]
        assert after and all(hit["build_id"] == updated["build_id"] for hit in after)
        updated_answer = expect(client.post("/api/v1/qa", json=ask), 200)
        assert "关闭空调" in updated_answer["answer"] and "关闭照明" not in updated_answer["answer"]
        runtime.say("[10/10] Delete with Worker paused: block access BEFORE physical cleanup")
        runtime.stop_worker()
        deletion = expect(client.delete(f"/api/v1/documents/{uploaded['document_id']}"), 202)
        for reference in (citation, updated_answer["citations"][0]):
            expect(client.get(f"/api/v1/chunks/{reference['chunk_id']}"), 404)
        assert expect(client.post("/api/v1/search", json=query), 200)["hits"] == []
        deleted_answer = expect(client.post("/api/v1/qa", json=ask), 200)
        assert deleted_answer["answer_status"] == "no_evidence"
        checks["deleted_reference"] = 404
        checks["deleted_search_hits"] = 0
        runtime.start_worker()
        wait_job(client, deletion["cleanup_job_id"], progress=runtime.say)
        expect(client.get(f"/api/v1/documents/{uploaded['document_id']}"), 404)
        runtime.say("[result] PASS: all 10 lifecycle steps verified through real HTTP")
        return {
            "question": QUESTION,
            "answer": answer,
            "updated_answer": updated_answer,
            "source": chunk,
            "missing": missing,
            "checks": checks,
        }
