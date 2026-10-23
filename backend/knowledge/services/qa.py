"""问答用例：证据门槛 → 生成 → 引用校验 → 授权复核 → 存档。"""

from __future__ import annotations

import uuid

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.entities import (
    Actor,
    Answer,
    AnswerStatus,
    Citation,
    Evidence,
    GeneratedAnswer,
    SearchHit,
)
from knowledge.domain.errors import CitationValidationFailed, SourceChanged
from knowledge.domain.ports import Embedder, Generator
from knowledge.logging import get_logger
from knowledge.repositories import conversations as conversation_repo
from knowledge.services import retrieval

EXCERPT_CHARS = 200

log = get_logger("qa")


def _build_evidence(hits: list[SearchHit], limit: int) -> list[Evidence]:
    return [
        Evidence(
            key=f"E{index}",
            chunk_id=hit.chunk_id,
            document_id=hit.document_id,
            version_id=hit.version_id,
            build_id=hit.build_id,
            title=hit.title,
            text=hit.text,
            locator=hit.locator,
        )
        for index, hit in enumerate(hits[:limit], start=1)
    ]


def _sources_unchanged(session: Session, hits: list[SearchHit]) -> bool:
    """复核引用的文档仍可访问，且生效构建未被替换。"""
    if not hits:
        return True
    ids = list({uuid.UUID(hit.document_id) for hit in hits})
    statement = text(
        "SELECT id, status, active_build_id FROM documents WHERE id IN :ids"
    ).bindparams(bindparam("ids", expanding=True))
    rows = session.execute(statement, {"ids": ids}).mappings().all()
    current = {str(row["id"]): row for row in rows}

    for hit in hits:
        row = current.get(hit.document_id)
        if row is None or row["status"] != "active":
            return False
        if str(row["active_build_id"]) != hit.build_id:
            return False
    return True


def _assemble(
    generated: GeneratedAnswer, evidence: list[Evidence]
) -> tuple[str, list[Citation], list[str]]:
    valid = {item.key: item for item in evidence}
    warnings: list[str] = []
    citations: list[Citation] = []
    seen: dict[str, int] = {}
    parts: list[str] = []

    for section in generated.sections:
        keys = [key for key in section.evidence_keys if key in valid]
        unknown = [key for key in section.evidence_keys if key not in valid]
        if unknown:
            warnings.append(f"已丢弃 {len(unknown)} 个不存在的证据键")
        if not keys:
            warnings.append("存在未标注证据的段落")
            parts.append(section.text)
            continue

        markers = []
        for key in keys:
            if key not in seen:
                item = valid[key]
                seen[key] = len(seen) + 1
                citations.append(
                    Citation(
                        index=seen[key],
                        document_id=item.document_id,
                        version_id=item.version_id,
                        build_id=item.build_id,
                        chunk_id=item.chunk_id,
                        title=item.title,
                        excerpt=item.text.strip()[:EXCERPT_CHARS],
                        locator=item.locator,
                    )
                )
            markers.append(f"[{seen[key]}]")
        parts.append(section.text + "".join(markers))

    if not citations:
        log.error("citation_validation_failed", extra={"sections": len(generated.sections)})
        raise CitationValidationFailed("答案未包含可校验的引用")

    return "\n\n".join(parts), citations, warnings


def answer(
    session: Session,
    actor: Actor,
    settings: Settings,
    embedder: Embedder,
    generator: Generator,
    *,
    question: str,
    kb_ids: list[uuid.UUID],
    top_k: int | None = None,
    conversation_id: uuid.UUID | None = None,
) -> Answer:
    hits = retrieval.search(
        session,
        actor,
        settings,
        embedder,
        query=question,
        kb_ids=kb_ids,
        mode="hybrid",
        top_k=top_k,
    )

    best = hits[0].score if hits else 0.0
    if not hits or best < settings.evidence_min_score:
        log.info("qa_no_evidence", extra={"hits": len(hits)})
        result = Answer(
            status=AnswerStatus.NO_EVIDENCE.value,
            text="资料中未找到相关依据。",
            hits=len(hits),
        )
        _persist(session, actor, conversation_id, question, result, model_revision="")
        return result

    evidence = _build_evidence(hits, settings.evidence_max_items)
    generated = generator.generate(question, evidence)
    body, citations, warnings = _assemble(generated, evidence)

    if not _sources_unchanged(session, hits):
        log.warning("qa_source_changed", extra={"hits": len(hits)})
        raise SourceChanged("资料已更新或不可访问，请重新提问")

    log.info(
        "qa_answered",
        extra={
            "hits": len(hits),
            "citations": len(citations),
            "warnings": len(warnings),
            "model_revision": generated.model_revision,
        },
    )
    result = Answer(
        status=AnswerStatus.ANSWERED.value,
        text=body,
        citations=citations,
        warnings=warnings,
        hits=len(hits),
    )
    _persist(session, actor, conversation_id, question, result, generated.model_revision)
    return result


def _persist(
    session: Session,
    actor: Actor,
    conversation_id: uuid.UUID | None,
    question: str,
    result: Answer,
    model_revision: str,
) -> None:
    if conversation_id is None:
        return
    conversation_repo.get_owned(session, actor, conversation_id)
    conversation_repo.append(
        session,
        conversation_id,
        question=question,
        answer=result.text,
        answer_status=result.status,
        citations=[citation.to_dict() for citation in result.citations],
        model_revision=model_revision,
    )
    session.commit()
