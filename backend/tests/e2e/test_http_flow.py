from __future__ import annotations

import io
import time

import pytest

from scripts.ask_console import run_console
from scripts.http_scenario import expect, run_lifecycle, upload_sample, wait_job


def test_http_lifecycle_with_independent_worker(runtime):
    result = run_lifecycle(runtime)
    assert result["answer"]["answer_status"] == "answered"
    assert result["updated_answer"]["answer_status"] == "answered"
    assert result["checks"]["deleted_reference"] == 404


def test_terminal_question_returns_http_answer_and_citation(runtime):
    with runtime.client() as client:
        kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "console-test"}), 201)
        uploaded = upload_sample(client, kb["id"])
        wait_job(client, uploaded["job_id"], progress=runtime.say)
        questions = iter(["会议室使用后需要做什么？", "/quit"])
        output = io.StringIO()
        run_console(client, kb["id"], read=lambda _: next(questions), output=output)
        assert "关闭照明" in output.getvalue()
        assert "引用 [1] meeting-room.md" in output.getvalue()
        assert "字符 [" in output.getvalue()
        runtime.say("[console] PASS: terminal question -> real HTTP -> server citation")


def test_real_worker_crash_before_lease_expiry_recovers(runtime):
    with runtime.client() as client:
        kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "crash-test"}), 201)
        runtime.model_fixture.block_embeddings()
        uploaded = upload_sample(client, kb["id"])
        assert runtime.model_fixture.embedding_entered.wait(20), "Worker never reached embedding"
        before = expect(client.get(f"/api/v1/jobs/{uploaded['job_id']}"), 200)
        assert before["state"] == "running" and before["stage"] == "embedding"
        assert before["attempt"] == 1
        runtime.stop_worker(force=True)
        runtime.model_fixture.release_embeddings()
        runtime.start_worker()
        runtime.say("[crash] Worker killed during embedding; restarted before lease expiry")
        job = wait_job(client, uploaded["job_id"], progress=runtime.say)
        assert job["attempt"] == 2
        document = expect(client.get(f"/api/v1/documents/{uploaded['document_id']}"), 200)
        assert document["active_build_id"] != uploaded["build_id"]
        runtime.say("[crash] PASS: real process restart, attempt=2, new build published")


def test_reindex_http_keeps_old_publication_and_rejects_duplicate(runtime):
    with runtime.client() as client, runtime.client("reindex-other") as other:
        kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "reindex-http"}), 201)
        old = upload_sample(client, kb["id"])
        wait_job(client, old["job_id"], progress=runtime.say)
        endpoint = f"/api/v1/documents/{old['document_id']}/reindex"
        body = {"version_id": old["version_id"], "expected_generation": 1}
        runtime.say("[reindex 1/5] 拒绝跨用户重建及身份字段注入")
        expect(other.post(endpoint, json=body), 404)
        expect(client.post(endpoint, json={**body, "owner_id": "other"}), 422)
        schema = client.get("/openapi.json")
        assert schema.status_code == 200
        assert "/api/v1/documents/{document_id}/reindex" in schema.json()["paths"]
        runtime.stop_worker()
        runtime.say("[reindex 2/5] 暂停 Worker，创建新 build/job，不新增版本")
        new = expect(client.post(endpoint, json=body), 202)
        assert new["generation"] == 2 and new["version_id"] == old["version_id"]
        assert new["build_id"] != old["build_id"] and new["job_id"] != old["job_id"]
        assert set(new) == {"document_id", "version_id", "build_id", "job_id", "generation"}
        runtime.say("[reindex 3/5] 重复旧请求及有未结束任务时均返回 409")
        assert expect(client.post(endpoint, json=body), 409)["code"] == "VERSION_CONFLICT"
        expect(client.post(endpoint, json={**body, "expected_generation": 2}), 409)
        query = {"question": "会议室使用后需要做什么？", "knowledge_base_ids": [kb["id"]]}
        answer = expect(client.post("/api/v1/qa", json=query), 200)
        assert answer["citations"][0]["build_id"] == old["build_id"]
        runtime.say("[reindex 4/5] 重建期间旧答案/引用可用；启动 Worker 后原子切换")
        runtime.start_worker()
        wait_job(client, new["job_id"], progress=runtime.say)
        answer = expect(client.post("/api/v1/qa", json=query), 200)
        assert answer["citations"][0]["build_id"] == new["build_id"]
        assert answer["citations"][0]["version_id"] == old["version_id"]
        runtime.say("[reindex 5/5] 删除提交后立即拒绝重建，不依赖异步清理")
        runtime.stop_worker()
        expect(client.delete(f"/api/v1/documents/{old['document_id']}"), 202)
        expect(client.post(endpoint, json={**body, "expected_generation": 3}), 404)
        runtime.start_worker()
        runtime.say("[reindex] PASS: 同模型重建、旧发布可用、重复请求与删除阻断")


def test_exhausted_job_recovers_via_public_reindex(runtime):
    with runtime.client() as client:
        kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "recovery-http"}), 201)
        old = upload_sample(client, kb["id"])
        wait_job(client, old["job_id"], progress=runtime.say)
        runtime.say("[recovery 1/4] 模拟向量服务故障，独立 Worker 按正常退避耗尽自动重试")
        runtime.model_fixture.embedding_failure = True
        latest = upload_sample(client, kb["id"], version=2, document_id=old["document_id"])
        deadline = time.monotonic() + 90
        observed = None
        while time.monotonic() < deadline:
            failed = expect(client.get(f"/api/v1/jobs/{latest['job_id']}"), 200)
            state = (failed["state"], failed["attempt"])
            if state != observed:
                runtime.say(f"[recovery job] state={state[0]} attempt={state[1]}")
                observed = state
            if failed["state"] == "failed":
                break
            assert failed["state"] != "succeeded", "Injected outage did not reach Worker"
            time.sleep(0.5)
        else:
            pytest.fail("正常自动重试未在期限内进入 failed")
        assert failed["attempt"] == failed["max_attempts"] == 3
        assert failed["last_error"]["code"] == "MODEL_UNAVAILABLE"
        runtime.model_fixture.embedding_failure = False
        runtime.stop_worker()
        runtime.say("[recovery 2/4] 故障排除，使用失败版本的回执 ID 创建新恢复任务")
        target = f"/api/v1/documents/{old['document_id']}"
        before = expect(client.get(target), 200)
        assert before["current_version_id"] == old["version_id"]
        recovered = expect(
            client.post(
                target + "/reindex",
                json={
                    "version_id": latest["version_id"],
                    "expected_generation": before["generation"],
                },
            ),
            202,
        )
        assert recovered["job_id"] != latest["job_id"]
        query = {"question": "会议室使用后需要做什么？", "knowledge_base_ids": [kb["id"]]}
        answer = expect(client.post("/api/v1/qa", json=query), 200)
        assert answer["citations"][0]["build_id"] == old["build_id"]
        runtime.say("[recovery 3/4] 恢复期间旧引用仍有效；重启 Worker 执行新 job")
        runtime.start_worker()
        job = wait_job(client, recovered["job_id"], progress=runtime.say)
        assert job["attempt"] == 1 and job["kind"] == "reindex"
        answer = expect(client.post("/api/v1/qa", json=query), 200)
        assert answer["citations"][0]["version_id"] == latest["version_id"]
        assert answer["citations"][0]["build_id"] == recovered["build_id"]
        assert expect(client.get(f"/api/v1/jobs/{latest['job_id']}"), 200)["state"] == "failed"
        runtime.say("[recovery 4/4] PASS: 自动重试耗尽 -> 公开接口恢复 -> 新版本引用生效")


@pytest.mark.parametrize("failure", ["503", "invalid-envelope", "invalid-sections"])
def test_upstream_failure_is_not_no_evidence(runtime, failure):
    with runtime.client() as client:
        kb = expect(client.post("/api/v1/knowledge-bases", json={"name": "upstream-test"}), 201)
        uploaded = upload_sample(client, kb["id"])
        wait_job(client, uploaded["job_id"], progress=runtime.say)
        runtime.model_fixture.generator_failure = failure
        response = client.post(
            "/api/v1/qa",
            json={
                "question": "会议室使用后需要做什么？",
                "knowledge_base_ids": [kb["id"]],
            },
        )
        expected = 502 if failure == "invalid-sections" else 503
        assert response.status_code == expected
        assert "data" not in response.json()
        assert response.json()["error"]["code"] == (
            "CITATION_VALIDATION_FAILED" if expected == 502 else "MODEL_UNAVAILABLE"
        )
        runtime.say(f"[upstream] PASS: {failure} -> HTTP {expected}, not no_evidence")
