"""端到端演示：建库 → 上传 → 处理 → 检索 → 问答 → 分块定位。

用法：
    python scripts/demo_flow.py [--base-url http://127.0.0.1:8010] [--file testdata/engineering/meeting-room.md]
"""

from __future__ import annotations

import argparse
import sys
import time
import uuid

import httpx

from knowledge.security import issue_token

QUESTION = "会议室使用结束后需要做什么？"


def _show(title: str) -> None:
    print(f"\n=== {title} " + "=" * max(0, 46 - len(title)))


def _data(response: httpx.Response) -> dict:
    if response.status_code >= 400:
        print(f"请求失败 {response.status_code}: {response.text[:400]}")
        raise SystemExit(1)
    return response.json()["data"]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default="http://127.0.0.1:8010")
    parser.add_argument("--file", default="testdata/engineering/meeting-room.md")
    parser.add_argument("--subject", default=f"demo-{uuid.uuid4().hex[:6]}")
    args = parser.parse_args()

    token = issue_token(args.subject)
    # trust_env=False：本地请求不走系统代理，否则会被代理截走
    client = httpx.Client(
        base_url=args.base_url,
        timeout=30,
        trust_env=False,
        headers={"Authorization": f"Bearer {token}"},
    )

    _show("建库")
    kb = _data(client.post("/api/v1/knowledge-bases", json={"name": "制度库"}))
    print(f"kb_id={kb['id']}  status={kb['status']}")

    _show("上传")
    with open(args.file, "rb") as handle:
        upload = _data(
            client.post(
                f"/api/v1/knowledge-bases/{kb['id']}/documents",
                files={"file": (args.file.split("/")[-1], handle, "text/markdown")},
            )
        )
    print(f"document_id={upload['document_id']}  job_id={upload['job_id']}  dedup={upload['deduplicated']}")

    _show("轮询任务")
    deadline = time.time() + 30
    job = {}
    while time.time() < deadline:
        job = _data(client.get(f"/api/v1/jobs/{upload['job_id']}"))
        print(f"  state={job['state']:<10} stage={job['stage']}")
        if job["state"] in ("succeeded", "failed", "cancelled"):
            break
        time.sleep(1)
    if job.get("state") != "succeeded":
        print("任务未成功，终止演示")
        return 1

    _show("文档状态")
    detail = _data(client.get(f"/api/v1/documents/{upload['document_id']}"))
    print(
        f"status={detail['status']}  version_no={detail['version_no']}  "
        f"active_build={detail['active_build_id'] is not None}  pending={detail['pending_build_id']}"
    )

    _show("检索")
    hits = _data(
        client.post(
            "/api/v1/search",
            json={"query": QUESTION, "knowledge_base_ids": [kb["id"]], "mode": "hybrid", "top_k": 3},
        )
    )
    for index, hit in enumerate(hits["hits"], start=1):
        print(f"  [{index}] score={hit['score']:.4f} ({hit['score_type']})  locator={hit['locator']}")
        print(f"      {hit['excerpt'][:60]}")

    _show("问答")
    answer = _data(
        client.post("/api/v1/qa", json={"question": QUESTION, "knowledge_base_ids": [kb["id"]]})
    )
    print(f"answer_status={answer['answer_status']}")
    print(f"answer: {answer['answer']}")
    for citation in answer["citations"]:
        print(f"  [{citation['index']}] {citation['title']}  locator={citation['locator']}")
        print(f"      excerpt: {citation['excerpt'][:60]}")
    if answer["warnings"]:
        print(f"warnings: {answer['warnings']}")

    _show("引用回溯")
    if answer["citations"]:
        chunk = _data(client.get(f"/api/v1/chunks/{answer['citations'][0]['chunk_id']}"))
        print(f"chunk_id={chunk['chunk_id']}")
        print(f"document={chunk['document_id']}  version={chunk['version_id']}")
        print(f"text={chunk['text'][:80]}")

    _show("无答案的提问")
    missing = _data(
        client.post("/api/v1/qa", json={"question": "年假报销上限是多少？", "knowledge_base_ids": [kb["id"]]})
    )
    print(f"answer_status={missing['answer_status']}  answer={missing['answer']}")

    _show("越权访问")
    other = issue_token("intruder")
    denied = httpx.get(
        f"{args.base_url}/api/v1/documents/{upload['document_id']}",
        headers={"Authorization": f"Bearer {other}"},
        trust_env=False,
    )
    print(f"status={denied.status_code}  body={denied.json()['error']['code']}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
