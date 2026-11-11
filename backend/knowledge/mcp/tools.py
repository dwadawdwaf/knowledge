"""MCP 工具面：6 个工具的参数与返回映射，业务全部复用 services。

协议实现（stdio / SSE）在 M2 接入，本模块先固定工具名与签名，避免届时改动业务层。
"""

from __future__ import annotations

import uuid
from typing import Any

from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.domain.errors import public_job_error
from knowledge.domain.ports import Embedder, Generator, ObjectStorage
from knowledge.repositories import chunks as chunk_repo
from knowledge.repositories import jobs as job_repo
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import qa as qa_service
from knowledge.services import retrieval

TOOLS: dict[str, str] = {
    "kb_list": "列出当前用户可访问的知识库",
    "doc_ingest": "上传一个文件到指定知识库，返回任务 ID",
    "job_status": "查询文档处理任务的状态与阶段",
    "search": "在指定知识库范围内检索片段，不生成答案",
    "ask": "在指定知识库范围内回答问题，返回带引用的答案",
    "chunk_get": "读取一个分块原文及其在文档中的位置",
}


def kb_list(session: Session, actor: Actor) -> dict[str, Any]:
    return {
        "items": [
            {"id": str(kb.id), "name": kb.name, "status": kb.status.value}
            for kb in knowledge_service.list_owned(session, actor)
        ]
    }


def doc_ingest(
    session: Session,
    actor: Actor,
    settings: Settings,
    storage: ObjectStorage,
    registry,
    embedder: Embedder,
    *,
    kb_id: uuid.UUID,
    filename: str,
    data: bytes,
) -> dict[str, Any]:
    result = document_service.upload(
        session,
        actor,
        settings,
        storage,
        registry,
        kb_id=kb_id,
        filename=filename,
        data=data,
        embedder_model=embedder.model,
        embedder_dim=embedder.dimension,
    )
    return {
        "document_id": str(result.document_id),
        "job_id": str(result.job_id),
        "deduplicated": result.deduplicated,
    }


def job_status(session: Session, actor: Actor, job_id: uuid.UUID) -> dict[str, Any]:
    job = job_repo.get_owned(session, actor, job_id)
    return {
        "id": str(job.id),
        "state": job.state.value,
        "stage": job.stage,
        "attempt": job.attempt,
        "last_error": public_job_error(job.last_error),
    }


def search(
    session: Session,
    actor: Actor,
    settings: Settings,
    embedder: Embedder,
    *,
    query: str,
    kb_ids: list[uuid.UUID],
    mode: str = "hybrid",
    top_k: int | None = None,
) -> dict[str, Any]:
    query_warnings: set[str] = set()
    hits = retrieval.search(
        session, actor, settings, embedder, query=query, kb_ids=kb_ids, mode=mode, top_k=top_k,
        query_warnings=query_warnings,
    )
    warnings = sorted(query_warnings | {warning for hit in hits for warning in hit.warnings})
    return {
        "warnings": warnings,
        "degraded": bool(warnings),
        "hits": [
            {
                "document_id": hit.document_id,
                "kb_id": hit.kb_id,
                "kb_name": hit.kb_name,
                "chunk_id": hit.chunk_id,
                "title": hit.title,
                "excerpt": hit.excerpt,
                "score": round(hit.score, 6),
                "score_type": hit.score_type,
            }
            for hit in hits
        ]
    }


def ask(
    session: Session,
    actor: Actor,
    settings: Settings,
    embedder: Embedder,
    generator: Generator,
    *,
    question: str,
    kb_ids: list[uuid.UUID],
    top_k: int | None = None,
) -> dict[str, Any]:
    result = qa_service.answer(
        session,
        actor,
        settings,
        embedder,
        generator,
        question=question,
        kb_ids=kb_ids,
        top_k=top_k,
    )
    return {
        "answer_status": result.status,
        "answer": result.text,
        "citations": [citation.to_dict() for citation in result.citations],
        "warnings": result.warnings,
    }


def chunk_get(session: Session, actor: Actor, chunk_id: uuid.UUID) -> dict[str, Any]:
    return chunk_repo.get_owned(session, actor, chunk_id)
