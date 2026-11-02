"""本机持久库评测：显式备份清空、自制资料入库、限额实验及前端结果发布。"""

from __future__ import annotations

import argparse
import hashlib
import io
import json
import os
import subprocess
import sys
import time
import unicodedata
import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from statistics import mean
from typing import Any, Literal

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import httpx
import psutil
from docx import Document
from openpyxl import Workbook
from psycopg import Error as DatabaseError
from pydantic import BaseModel, ConfigDict, Field, model_validator
from sqlalchemy.engine import make_url

from scripts import local_db
from scripts.http_scenario import expect, wait_job
from scripts.local_runtime import BACKEND, WORKBENCH_OBJECTS, LocalRuntime
from scripts.real_model import load_real_settings
from scripts.workbench import open_workbench

DATASET = BACKEND / "testdata" / "retrieval_baseline" / "dataset.json"
TABLES = (
    "knowledge_bases",
    "documents",
    "document_versions",
    "index_builds",
    "chunks",
    "jobs",
    "conversations",
    "messages",
)
REPORT_LIBRARY = "检索评测 · 过程与结果"
REPORT_FILE = "retrieval-evaluation-v1.txt"
VARIANTS = (("keyword", 5), ("semantic", 5), ("hybrid", 5), ("hybrid", 20))
MAX_REQUESTS = 120


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    document: str
    quote: str = Field(min_length=1)
    aliases: list[str] = Field(min_length=1)
    pdf_pages: list[int] = Field(default_factory=list)
    normalized: bool = False


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: str
    question: str = Field(min_length=1, max_length=2000)
    facts: list[Fact]
    forbidden: list[str] = Field(default_factory=list)
    partition: Literal["development", "holdout"] = "development"


class Source(BaseModel):
    model_config = ConfigDict(extra="forbid")
    key: str
    name: str
    text: str = ""
    rows: list[list[str]] = Field(default_factory=list)


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    documents: list[Source]
    cases: list[Case] = Field(min_length=1, max_length=48)

    @model_validator(mode="after")
    def validate_labels(self) -> Dataset:
        sources = {source.key: source for source in self.documents}
        if len(sources) != len(self.documents) or len({c.id for c in self.cases}) != len(
            self.cases
        ):
            raise ValueError("资料和问题编号必须唯一")
        for case in self.cases:
            for fact in case.facts:
                source = sources[fact.document]
                text = source.text + json.dumps(source.rows, ensure_ascii=False)
                if not contains_quote(fact, text):
                    raise ValueError("标注证据不在自制资料中")
                if any(not alias.strip() for alias in fact.aliases):
                    raise ValueError("答案匹配词不能为空")
        return self


def normalize_text(text: str) -> str:
    """公开PDF标注可显式忽略排版空白及Unicode兼容字形，不改业务索引文本。"""
    return "".join(unicodedata.normalize("NFKC", text).casefold().split())


def contains_quote(fact: Fact, text: str) -> bool:
    if fact.normalized:
        return normalize_text(fact.quote) in normalize_text(text)
    return fact.quote in text


def supports_fact(fact: Fact, text: str, locator: dict) -> bool:
    return contains_quote(fact, text) and (
        not fact.pdf_pages or locator.get("page") in fact.pdf_pages
    )


@dataclass(frozen=True)
class ExperimentSpec:
    """单次实验的固定边界；默认值保持第一轮自制基线不变。"""

    kb_name: str = "基础检索评测 · 自制跨领域资料"
    report_file: str = REPORT_FILE
    request_limit: int = MAX_REQUESTS
    add_noise: bool = True
    publish_every: int = 6
    upload_timeout: int = 180
    title: str = "第一轮 · 自制基础集"
    dataset_sha256: str = ""
    compact_report: bool = False
    parser_profile: str = "legacy-v1"


def say(message: str) -> None:
    print(message, flush=True)


def validate_target(url: str, objects: Path) -> None:
    """重置只允许启动器记载的本机业务库及其项目原件卷。"""
    target = make_url(url)
    recorded = make_url(local_db.DB_URL_FILE.read_text(encoding="utf-8").strip())
    if target != recorded or target.host != "127.0.0.1" or target.database != "knowledge":
        raise ValueError("拒绝重置：不是启动器记录的本机knowledge库")
    expected = (BACKEND / ".data" / "objects").absolute()
    if objects.resolve() != expected or objects.is_symlink():
        raise ValueError("拒绝重置：原件目录不符合本项目持久卷约束")


def stop_project_services(url: str, objects: Path) -> None:
    """只停止连接目标库且使用目标原件卷的本项目API/Worker，不停止数据库。"""
    matched = []
    for process in psutil.process_iter(["pid", "name", "cmdline"]):
        command = " ".join(process.info["cmdline"] or [])
        if not any(
            name in command
            for name in ("knowledge.entrypoints.api", "knowledge.entrypoints.worker")
        ):
            continue
        try:
            env = process.environ()
            if env.get("KS_DATABASE_URL") != url:
                continue
            if Path(env.get("KS_STORAGE_ROOT", "")).resolve() != objects.resolve():
                raise ValueError("目标库还有使用其他原件卷的进程，请先手动退出")
            matched.append(process)
        except (psutil.NoSuchProcess, psutil.AccessDenied) as exc:
            raise ValueError("无法确认项目进程归属，请先退出旧启动终端") from exc
    for process in matched:
        say(f"[准备] 停止目标库的项目服务 PID={process.pid}")
        process.terminate()
    _, alive = psutil.wait_procs(matched, timeout=15)
    if alive:
        raise ValueError("旧服务尚未退出，不继续清空")


def backup_and_reset(url: str, objects: Path) -> Path:
    """完整备份恢复验证通过后才清空；迁移版本/扩展保留，旧原件移入配套备份。"""
    import psycopg
    from psycopg import sql

    validate_target(url, objects)
    stop_project_services(url, objects)
    target = make_url(url)
    connect_url = target.set(drivername="postgresql").render_as_string(hide_password=False)
    backup = BACKEND / ".data" / "evaluation-backups" / datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    backup.mkdir(parents=True, exist_ok=False)
    env = {
        **os.environ,
        "PGHOST": str(target.host),
        "PGPORT": str(target.port or 5432),
        "PGUSER": str(target.username or "postgres"),
        "PGPASSWORD": target.password or "",
    }

    def counts(connection: psycopg.Connection) -> dict[str, int]:
        result = {}
        for table in TABLES:
            row = connection.execute(
                sql.SQL("SELECT count(*) FROM {}").format(sql.Identifier(table))
            ).fetchone()
            assert row is not None
            result[table] = int(row[0])
        return result

    with psycopg.connect(connect_url, autocommit=True) as connection:
        present = {
            row[0]
            for row in connection.execute(
                "SELECT tablename FROM pg_tables WHERE schemaname='public'"
            )
        }
        if present != set(TABLES) | {"alembic_version"}:
            raise ValueError("业务表集合变化，拒绝按旧清单清空")
        before = counts(connection)
        version_row = connection.execute("SELECT version_num FROM alembic_version").fetchone()
        assert version_row is not None
        version = version_row[0]
        active_row = connection.execute(
            "SELECT count(*) FROM pg_stat_activity WHERE datname=current_database() AND pid<>pg_backend_pid() AND backend_type='client backend'"
        ).fetchone()
        assert active_row is not None
        if active_row[0]:
            raise ValueError("目标库仍有其他连接，请先退出项目及数据库工具")
        say(f"[备份] 业务表={len(before)}，总记录={sum(before.values())}；备份仅留本地")
        dump = backup / "before.dump"
        result = subprocess.run(
            [
                str(local_db.bin_dir() / "pg_dump.exe"),
                "-Fc",
                "--file",
                str(dump),
                "--dbname",
                str(target.database),
            ],
            check=False,
            env=env,
            capture_output=True,
            timeout=180,
            creationflags=local_db._NO_WINDOW,
        )
        if result.returncode:
            raise RuntimeError("数据库备份失败，未清空")
        replica = "eval_verify_" + uuid.uuid4().hex[:12]
        connection.execute(sql.SQL("CREATE DATABASE {}").format(sql.Identifier(replica)))
        try:
            restored = subprocess.run(
                [
                    str(local_db.bin_dir() / "pg_restore.exe"),
                    "--exit-on-error",
                    "--no-owner",
                    "--dbname",
                    replica,
                    str(dump),
                ],
                check=False,
                env=env,
                capture_output=True,
                timeout=180,
                creationflags=local_db._NO_WINDOW,
            )
            if restored.returncode:
                raise RuntimeError("备份恢复验证失败，未清空")
            replica_url = target.set(drivername="postgresql", database=replica).render_as_string(
                hide_password=False
            )
            with psycopg.connect(replica_url) as check:
                restored_version = check.execute(
                    "SELECT version_num FROM alembic_version"
                ).fetchone()
                if counts(check) != before or restored_version != version_row:
                    raise RuntimeError("恢复副本记录或版本不一致，未清空")
        finally:
            connection.execute(
                sql.SQL("DROP DATABASE {} WITH (FORCE)").format(sql.Identifier(replica))
            )
        say("[备份] 已实际恢复到独立副本并核对计数与迁移版本")
        archived = backup / "objects"
        moved = False
        try:
            with connection.transaction():
                connection.execute("SET LOCAL lock_timeout='5s'")
                connection.execute(
                    sql.SQL("TRUNCATE {} RESTART IDENTITY").format(
                        sql.SQL(",").join(sql.Identifier(table) for table in TABLES)
                    )
                )
                if objects.exists():
                    objects.rename(archived)
                    moved = True
                objects.mkdir(parents=True, exist_ok=True)
        except BaseException:
            if moved:
                objects.rmdir()
                archived.rename(objects)
            raise
        if any(counts(connection).values()):
            raise RuntimeError("清空后计数不为零")
    manifest = {
        "counts_before": before,
        "schema": version,
        "dump_sha256": hashlib.sha256(dump.read_bytes()).hexdigest(),
        "restore_verified": True,
        "objects_archived": moved,
    }
    (backup / "manifest.json").write_text(json.dumps(manifest, indent=2), encoding="utf-8")
    say(f"[清空] 8张业务表已清空，旧原件与数据库备份在 {backup}")
    return backup


def source_bytes(source: Source) -> bytes:
    """仅生成自制文件，复用已有Office库，不依赖OCR/外网抓取。"""
    output = io.BytesIO()
    if source.name.endswith(".docx"):
        doc = Document()
        for line in source.text.splitlines():
            doc.add_paragraph(line)
        doc.save(output)
        return output.getvalue()
    if source.name.endswith(".xlsx"):
        workbook = Workbook()
        sheet = workbook.active
        assert sheet is not None
        for row in source.rows:
            sheet.append(row)
        workbook.save(output)
        return output.getvalue()
    if source.name.endswith(".csv"):
        import csv

        text = io.StringIO(newline="")
        csv.writer(text).writerows(source.rows)
        return text.getvalue().encode("utf-8-sig")
    return source.text.encode("utf-8")


# HTTP响应和持久报告为不同模式的嵌套JSON；边界由现有API和本模块数据集模型校验。
Json = dict[str, Any]


def retrieval_metrics(case: Case, hits: list[Json], sources: Json) -> Json:
    ranks = [
        next(
            (
                rank
                for rank, hit in enumerate(hits, 1)
                if hit["document_id"] == sources[fact.document]["document_id"]
                and hit["build_id"] == sources[fact.document]["build_id"]
                and supports_fact(fact, hit["excerpt"], hit.get("locator", {}))
            ),
            None,
        )
        for fact in case.facts
    ]
    found = [rank for rank in ranks if rank is not None]
    return {
        "hit_count": len(hits),
        "fact_recall": len(found) / len(ranks) if ranks else None,
        "mrr": 1 / min(found) if found else (0 if ranks else None),
        "all_facts_found": len(found) == len(ranks) if ranks else None,
    }


def summarize(cases: list[Json]) -> Json:
    summary: Json = {}
    for mode, size in VARIANTS:
        key = f"{mode}_{size}"
        positives = [case["retrieval"][key] for case in cases if case["answerable"]]
        valid = [row for row in positives if "error" not in row]
        summary[key] = {
            "positive_cases": len(positives),
            "valid_cases": len(valid),
            "errors": len(positives) - len(valid),
            "mean_fact_recall": mean(row["fact_recall"] for row in valid) if valid else None,
            "mrr": mean(row["mrr"] for row in valid) if valid else None,
            "complete_cases": sum(row["all_facts_found"] for row in valid),
        }
    return summary


def run_experiment(
    runtime: LocalRuntime,
    dataset: Dataset,
    *,
    report_directory: Path | None = None,
    spec: ExperimentSpec | None = None,
    source_loader: Callable[[Source], bytes] = source_bytes,
    preparation: Json | None = None,
    resume_ingestion: bool = False,
) -> Json:
    """问答经现有会话存档；结果作为独立知识库原件发布，不新增业务表。"""
    selected = spec or ExperimentSpec()
    if len(dataset.cases) * (len(VARIANTS) + 1) > selected.request_limit:
        raise ValueError("问题数超出本次批准的实验请求预算")
    report: Json = {
        "schema": "retrieval-evaluation-v1",
        "revision": dataset.revision,
        "dataset_sha256": selected.dataset_sha256
        or hashlib.sha256(DATASET.read_bytes()).hexdigest(),
        "title": selected.title,
        "preparation": preparation or {},
        "label_status": "developer_authored_pending_review",
        "status": "running",
        "request_limit": selected.request_limit,
        "requests": 0,
        "total_cases": len(dataset.cases),
        "cases": [],
        "events": [],
        "sources": [],
        "summary": {},
        "config": {
            key: runtime.env.get("KS_" + key.upper(), default)
            for key, default in (
                ("embedding_model", ""),
                ("generator_model", ""),
                ("similarity_floor", "0.06"),
                ("semantic_max_distance", "0.85"),
            )
        },
    }
    with runtime.client() as client:
        libraries = expect(client.get("/api/v1/knowledge-bases"), 200)["items"]
        if resume_ingestion:
            matches = [item for item in libraries if item["name"] == selected.kb_name]
            if len(matches) != 1:
                raise ValueError("恢复入库要求唯一的原评测库")
            kb = matches[0]
        else:
            kb = expect(
                client.post("/api/v1/knowledge-bases", json={"name": selected.kb_name}), 201
            )
        reports = next((kb for kb in libraries if kb["name"] == REPORT_LIBRARY), None)
        if reports is None:
            reports = expect(
                client.post("/api/v1/knowledge-bases", json={"name": REPORT_LIBRARY}), 201
            )
        if resume_ingestion:
            prior_reports = expect(
                client.get(f"/api/v1/knowledge-bases/{reports['id']}/documents"), 200
            )["items"]
            if any(item["title"] == selected.report_file for item in prior_reports):
                raise ValueError("已有本轮报告，禁止从零重跑问答")
        report["knowledge_base_id"] = kb["id"]
        sources: Json = {}
        report_document: Json = {}

        def event(stage: str, count: int) -> None:
            report["events"].append(
                {"stage": stage, "count": count, "at": datetime.now(UTC).isoformat()}
            )
            runtime.say(f"[评测过程] {stage} count={count}")

        def upload(kb_id: str, name: str, data: bytes, document: Json | None = None) -> Json:
            endpoint = (
                f"/api/v1/documents/{document['document_id']}/versions"
                if document
                else f"/api/v1/knowledge-bases/{kb_id}/documents"
            )
            response = client.post(
                endpoint,
                data={"parser_profile": selected.parser_profile},
                files={"file": (name, data)},
            )
            if resume_ingestion and document is None and response.status_code == 200:
                result = expect(response, 200)
                if result["parser_profile"] != selected.parser_profile:
                    raise ValueError("原资料解析策略不一致，拒绝恢复")
                info = expect(client.get(f"/api/v1/documents/{result['document_id']}"), 200)
                if (info.get("job") or {}).get("state") in {"queued", "running"}:
                    raise ValueError("原资料仍有未结束任务，请先核对状态")
                if info["active_build_id"]:
                    result["build_id"] = info["active_build_id"]
                    result["version_id"] = info["current_version_id"]
                    runtime.say("[入库恢复] 复用已发布构建，不新增版本或任务")
                    return result
                result = expect(
                    client.post(
                        f"/api/v1/documents/{result['document_id']}/reindex",
                        json={
                            "version_id": result["version_id"],
                            "expected_generation": info["generation"],
                        },
                    ),
                    202,
                )
                runtime.say("[入库恢复] 失败资料通过新build恢复，原版本不变")
            else:
                result = expect(response, 202)
            wait_job(
                client, result["job_id"], progress=runtime.say, timeout=selected.upload_timeout
            )
            return result

        def publish() -> None:
            nonlocal report_document
            report["updated_at"] = datetime.now(UTC).isoformat()
            report["summary"] = summarize(report["cases"])
            report["partition_summary"] = {
                partition: summarize(
                    [row for row in report["cases"] if row["partition"] == partition]
                )
                for partition in ("development", "holdout")
            }
            payload = json.dumps(
                report, ensure_ascii=False, indent=None if selected.compact_report else 2
            ).encode("utf-8")
            report_document = upload(
                reports["id"], selected.report_file, payload, report_document or None
            )
            directory = report_directory or BACKEND / ".data" / "retrieval-baseline"
            directory.mkdir(parents=True, exist_ok=True)
            (directory / "report.json").write_bytes(payload)

        def post(path: str, body: Json) -> httpx.Response:
            if report["requests"] >= selected.request_limit:
                raise ValueError("达到评测请求上限，不再调用")
            report["requests"] += 1
            return client.post(path, json=body)

        event("导入固定资料", 0)
        for number, source in enumerate(dataset.documents, 1):
            runtime.say(f"[资料 {number}/{len(dataset.documents)}] 格式={Path(source.name).suffix}")
            sources[source.key] = upload(kb["id"], source.name, source_loader(source))
            published = expect(
                client.get(
                    f"/api/v1/documents/{sources[source.key]['document_id']}/chunks",
                    params={"limit": 1},
                ),
                200,
            )
            report["sources"].append(
                {
                    "name": source.name,
                    "document_id": sources[source.key]["document_id"],
                    "build_id": sources[source.key]["build_id"],
                    "published_chunks": published["total"],
                }
            )
        # 干扰文档不对应任何金标事实，用于检验长段落、重复词和相近业务说明。
        noise = "本段为虚构的检索干扰资料，不包含评测问题的答案。" * 20
        noise_topics = (
            ("会议设施与办公采购", "设备更新与平台巡检", "差旅调研与资料归档")
            if selected.add_noise
            else ()
        )
        for number, topic in enumerate(noise_topics, 1):
            upload(kb["id"], f"干扰资料-{number}.md", (f"# {topic}\n" + noise).encode())
        event("资料发布完成", len(dataset.documents) + len(noise_topics))
        publish()
        open_workbench(runtime)
        conversations: Json = {}
        chunk_cache: Json = {}
        try:
            for number, case in enumerate(dataset.cases, 1):
                runtime.say(f"[实验 {number}/{len(dataset.cases)}] {case.id} / {case.category}")
                row: Json = {
                    "id": case.id,
                    "category": case.category,
                    "question": case.question,
                    "answerable": bool(case.facts),
                    "partition": case.partition,
                    "expected": [fact.model_dump() for fact in case.facts],
                    "retrieval": {},
                    "qa": {},
                }
                for mode, size in VARIANTS:
                    started = time.monotonic()
                    response = post(
                        "/api/v1/search",
                        {
                            "query": case.question,
                            "knowledge_base_ids": [kb["id"]],
                            "mode": mode,
                            "top_k": size,
                        },
                    )
                    key = f"{mode}_{size}"
                    if response.status_code != 200:
                        row["retrieval"][key] = {
                            "error": response.json().get("error", {}).get("code", "HTTP_ERROR"),
                            "http_status": response.status_code,
                        }
                        continue
                    data = expect(response, 200)
                    metrics = retrieval_metrics(case, data["hits"], sources)
                    metrics.update(
                        elapsed_ms=round((time.monotonic() - started) * 1000),
                        degraded=data["degraded"],
                        hits=[
                            {key: hit[key] for key in ("chunk_id", "document_id", "title", "score")}
                            for hit in data["hits"]
                        ],
                    )
                    row["retrieval"][key] = metrics
                if case.category not in conversations:
                    conversations[case.category] = expect(
                        client.post(
                            "/api/v1/conversations",
                            json={"title": selected.title + " · " + case.category},
                        ),
                        201,
                    )["id"]
                row["conversation_id"] = conversations[case.category]
                started = time.monotonic()
                response = post(
                    "/api/v1/qa",
                    {
                        "question": case.question,
                        "knowledge_base_ids": [kb["id"]],
                        "top_k": 5,
                        "conversation_id": row["conversation_id"],
                    },
                )
                if response.status_code != 200:
                    row["qa"] = {
                        "error": response.json().get("error", {}).get("code", "HTTP_ERROR"),
                        "http_status": response.status_code,
                    }
                else:
                    data = expect(response, 200)
                    cited = []
                    for citation in data["citations"]:
                        chunk_id = citation["chunk_id"]
                        if chunk_id not in chunk_cache:
                            chunk_cache[chunk_id] = expect(
                                client.get(f"/api/v1/chunks/{chunk_id}"), 200
                            )
                        chunk = chunk_cache[chunk_id]
                        if (
                            all(
                                str(chunk[field]) == str(citation[field])
                                for field in ("document_id", "version_id", "build_id")
                            )
                            and citation["excerpt"] in chunk["text"]
                            and chunk["locator"] == citation["locator"]
                        ):
                            cited.append(chunk)
                    row["qa"] = {
                        "status": data["answer_status"],
                        "status_matches_label": data["answer_status"]
                        == ("answered" if case.facts else "no_evidence"),
                        "lexical_coverage": mean(
                            any(
                                normalize_text(alias) in normalize_text(data["answer"])
                                if fact.normalized
                                else alias in data["answer"]
                                for alias in fact.aliases
                            )
                            for fact in case.facts
                        )
                        if case.facts
                        else None,
                        "citation_fact_coverage": mean(
                            any(
                                chunk["document_id"] == sources[fact.document]["document_id"]
                                and chunk["build_id"] == sources[fact.document]["build_id"]
                                and supports_fact(fact, chunk["text"], chunk["locator"])
                                for chunk in cited
                            )
                            for fact in case.facts
                        )
                        if case.facts
                        else None,
                        "citations_resolvable": len(cited) == len(data["citations"]),
                        "citation_count": len(data["citations"]),
                        "forbidden_detected": any(
                            word in data["answer"] for word in case.forbidden
                        ),
                        "elapsed_ms": round((time.monotonic() - started) * 1000),
                        "warnings": data["warnings"],
                    }
                report["cases"].append(row)
                event("完成问题", number)
                runtime.say(
                    f"[结果 {case.id}] hybrid@5覆盖={row['retrieval']['hybrid_5'].get('fact_recall')}；问答={row['qa'].get('status', row['qa'].get('error'))}"
                )
                if number % selected.publish_every == 0:
                    publish()
            report["status"] = "completed"
            event("实验完成", report["requests"])
        except BaseException:
            report["status"] = "interrupted"
            event("实验中断，未自动重跑", len(report["cases"]))
            publish()
            raise
        publish()
    return report


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--reset-current", action="store_true", help="明确授权备份后清空当前本机业务数据"
    )
    parser.add_argument(
        "--confirm-real",
        action="store_true",
        help="允许自制资料使用现有真实模型，最多120次检索/问答",
    )
    parser.add_argument("--serve", action="store_true", help="完成后继续保持前端服务")
    parser.add_argument("--port", type=int, default=8010)
    args = parser.parse_args()
    if not args.reset_current or not args.confirm_real:
        say("必须显式指定 --reset-current --confirm-real；未清空、未调用模型。")
        return 1
    dataset = Dataset.model_validate_json(DATASET.read_bytes())
    settings = load_real_settings()
    url = local_db.DB_URL_FILE.read_text(encoding="utf-8").strip()
    objects = WORKBENCH_OBJECTS.resolve()
    backup_and_reset(url, objects)
    with LocalRuntime(
        database_url=url, storage_root=str(objects), model_settings=settings, port=args.port
    ) as runtime:
        report = run_experiment(runtime, dataset)
        say(
            f"[完成] {len(report['cases'])}题 / {report['requests']}次请求；结果在前端检索评测中，业务效果仍需人工复核。"
        )
        if args.serve:
            say("[前端] 服务保持运行，Ctrl+C退出；资料与评测结果持久保留。")
            try:
                while True:
                    time.sleep(60)
            except KeyboardInterrupt:
                pass
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        say("[中止] 已停止，不自动重跑；已有备份与已发布结果保留。")
        raise SystemExit(130) from None
    except (
        ValueError,
        RuntimeError,
        OSError,
        AssertionError,
        httpx.HTTPError,
        DatabaseError,
        subprocess.SubprocessError,
    ) as exc:
        # 终端不输出驱动SQL、连接信息或模型异常正文；错误类型用于下一步单点诊断。
        say(f"[失败] 评测未完成，错误类型={type(exc).__name__}；未自动重跑，请检查最后完成的阶段。")
        raise SystemExit(1) from None
