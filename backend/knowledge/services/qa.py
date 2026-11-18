"""问答用例：上下文窗口 → 检索问题 → 证据门槛 → 生成 → 引用校验 → 授权复核 → 存档。

非流式（`answer`）与流式（`services/qa_stream.py`）共用同一套证据收集与引用校验，
区别只在生成阶段：前者一次拿到 JSON sections，后者逐块拿到带 `[E#]` 标记的正文。
"""

from __future__ import annotations

import uuid
from dataclasses import dataclass, field

from sqlalchemy import bindparam, text
from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.citations import MARKER, marker_keys, strip_markers
from knowledge.domain.entities import (
    Actor,
    Answer,
    AnswerStatus,
    Citation,
    ContextMessage,
    Evidence,
    GeneratedAnswer,
    SearchHit,
)
from knowledge.domain.errors import CitationValidationFailed, SourceChanged
from knowledge.domain.ports import Embedder, Generator
from knowledge.logging import get_logger
from knowledge.repositories import conversations as conversation_repo
from knowledge.services import context as context_service
from knowledge.services import query_rewrite, retrieval

EXCERPT_CHARS = 200
NO_EVIDENCE_TEXT = "资料中未找到相关依据。"
INSUFFICIENT_TEXT = "资料中未找到足够依据。"
EMPTY_ANSWER_TEXT = "所选资料中没有足够依据回答这个问题。可以换一种问法，或把检索范围放宽到更多知识库。"

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


def sources_unchanged(session: Session, hits: list[SearchHit]) -> bool:
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


def _cite(item: Evidence, seen: dict[str, int], citations: list[Citation]) -> str:
    """按首次出现顺序分配引用号，同一证据只登记一次。"""
    if item.key not in seen:
        seen[item.key] = len(seen) + 1
        citations.append(
            Citation(
                index=seen[item.key],
                document_id=item.document_id,
                version_id=item.version_id,
                build_id=item.build_id,
                chunk_id=item.chunk_id,
                title=item.title,
                excerpt=item.text.strip()[:EXCERPT_CHARS],
                locator=item.locator,
            )
        )
    return f"[{seen[item.key]}]"


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

        markers = "".join(_cite(valid[key], seen, citations) for key in keys)
        parts.append(section.text + markers)

    if not citations:
        log.error("citation_validation_failed", extra={"sections": len(generated.sections)})
        raise CitationValidationFailed("答案未包含可校验的引用")

    return "\n\n".join(parts), citations, warnings


def _has_unannotated_paragraph(raw: str, valid: dict[str, Evidence]) -> bool:
    """是否存在没有任何有效证据标记的非空段落（兼容行为：保留正文并告警）。"""
    for block in raw.split("\n\n"):
        if not strip_markers(block).strip():
            continue
        keys = [f"E{int(match.group(1))}" for match in MARKER.finditer(block)]
        if not any(key in valid for key in keys):
            return True
    return False


def assemble_marked(
    raw: str, evidence: list[Evidence]
) -> tuple[str, list[Citation], list[str]]:
    """校验流式正文：按 `[E#]` 标记映射引用，未知键丢弃，全无有效引用即失败。"""
    valid = {item.key: item for item in evidence}
    warnings: list[str] = []
    citations: list[Citation] = []
    seen: dict[str, int] = {}

    keys = marker_keys(raw)
    unknown = [key for key in keys if key not in valid]
    if unknown:
        warnings.append(f"已丢弃 {len(unknown)} 个不存在的证据键")
    for key in keys:
        if key in valid:
            _cite(valid[key], seen, citations)

    if not citations:
        log.error("citation_validation_failed", extra={"sections": len(raw)})
        raise CitationValidationFailed("答案未包含可校验的引用")

    if _has_unannotated_paragraph(raw, valid):
        warnings.append("存在未标注证据的段落")
    body = strip_markers(raw).strip()
    if not body:
        raise CitationValidationFailed("答案未包含可校验的引用")
    return body, citations, warnings


@dataclass
class EvidenceSet:
    """检索阶段的产物；流式与非流式共用，事件也直接取自这里。"""

    hits: int
    evidence: list[Evidence] = field(default_factory=list)
    evidence_hits: list[SearchHit] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    degraded: bool = False

    @property
    def empty(self) -> bool:
        return not self.evidence


def build_query(
    session: Session,
    actor: Actor,
    settings: Settings,
    *,
    question: str,
    conversation_id: uuid.UUID | None,
    window: list[ContextMessage] | None = None,
) -> tuple[list[ContextMessage], query_rewrite.StandaloneQuery]:
    """读取 Recent Window 并构造检索问题；两步分开是为了让流式路径逐步报告阶段。"""
    context = (
        window
        if window is not None
        else context_service.load_window(session, actor, settings, conversation_id, question)
    )
    query = query_rewrite.standalone_query(
        context,
        question,
        anchor_chars=settings.context_rewrite_max_chars,
        query_chars=settings.query_max_chars,
    )
    return context, query


def recall(
    session: Session,
    actor: Actor,
    settings: Settings,
    embedder: Embedder,
    *,
    query: str,
    kb_ids: list[uuid.UUID],
    top_k: int | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> EvidenceSet:
    """按给定检索问题召回并选择证据；是否达到证据门槛由调用方判断。"""
    query_warnings: set[str] = set()
    hits = retrieval.search(
        session,
        actor,
        settings,
        embedder,
        query=query,
        kb_ids=kb_ids,
        mode="hybrid",
        top_k=top_k,
        candidate_limit=settings.evidence_candidate_limit,
        document_ids=document_ids,
        query_warnings=query_warnings,
    )

    degraded = bool(query_warnings)
    best = float(hits[0].diagnostics.get("fusion_score", hits[0].score)) if hits else 0.0
    result = EvidenceSet(hits=len(hits), degraded=degraded)
    if not hits or best < settings.evidence_min_score:
        result.warnings = sorted(query_warnings)
        return result

    selected = retrieval.select_evidence(session, actor, settings, kb_ids, hits)
    if not selected:
        result.warnings = sorted(query_warnings)
        return result

    result.evidence_hits = selected
    result.evidence = _build_evidence(selected, settings.evidence_max_items)
    result.warnings = sorted(
        set(query_warnings) | {warning for hit in selected for warning in hit.warnings}
    )
    result.degraded = degraded or any(hit.warnings for hit in selected)
    return result


def recall_with_context(
    session: Session,
    actor: Actor,
    settings: Settings,
    embedder: Embedder,
    *,
    query: query_rewrite.StandaloneQuery,
    kb_ids: list[uuid.UUID],
    top_k: int | None = None,
    document_ids: list[uuid.UUID] | None = None,
) -> EvidenceSet:
    """按补全后的问题召回；仍然没有候选时，只用上文再取一次。

    拼上当前问题会稀释查询向量，弱向量模型下可能整轮落空；此时用上文（上一轮问题）
    单独召回比直接返回无证据更接近用户意图。只在上文补全过、且首次为空时才发生。
    """
    found = recall(
        session,
        actor,
        settings,
        embedder,
        query=query.text,
        kb_ids=kb_ids,
        top_k=top_k,
        document_ids=document_ids,
    )
    if found.hits or not query.rewritten or not query.anchor:
        return found
    log.info("qa_retrieval_context_fallback", extra={"reason": "rewritten_query_empty"})
    return recall(
        session,
        actor,
        settings,
        embedder,
        query=query.anchor,
        kb_ids=kb_ids,
        top_k=top_k,
        document_ids=document_ids,
    )


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
    document_ids: list[uuid.UUID] | None = None,
) -> Answer:
    window, query = build_query(
        session, actor, settings, question=question, conversation_id=conversation_id
    )
    found = recall_with_context(
        session,
        actor,
        settings,
        embedder,
        query=query,
        kb_ids=kb_ids,
        top_k=top_k,
        document_ids=document_ids,
    )

    if found.empty:
        log.info("qa_no_evidence", extra={"hits": found.hits})
        result = Answer(
            status=AnswerStatus.NO_EVIDENCE.value,
            text=NO_EVIDENCE_TEXT if found.hits == 0 else INSUFFICIENT_TEXT,
            hits=found.hits,
            warnings=found.warnings,
            degraded=found.degraded,
            context_messages=len(window),
            query_rewritten=query.rewritten,
        )
        persist(session, actor, conversation_id, question, result, model_revision="")
        return result

    generated = generator.generate(question, found.evidence, history=window)

    if not generated.sections:
        # 模型按约定判断证据不足，返回空 sections：这是正常拒答（C6 无证据不生成），
        # 不是「引用校验失败」。之前这里落到下游会抛 502，把正常拒答报成了系统错误。
        log.info("qa_no_evidence", extra={"hits": found.hits, "reason": "empty_sections"})
        result = Answer(
            status=AnswerStatus.NO_EVIDENCE.value,
            text=EMPTY_ANSWER_TEXT,
            hits=found.hits,
            warnings=found.warnings,
            degraded=found.degraded,
            context_messages=len(window),
            query_rewritten=query.rewritten,
        )
        persist(
            session, actor, conversation_id, question, result,
            model_revision=generated.model_revision,
        )
        return result

    body, citations, warnings = _assemble(generated, found.evidence)
    warnings = sorted(set(warnings) | set(found.warnings))

    if not sources_unchanged(session, found.evidence_hits):
        log.warning("qa_source_changed", extra={"hits": found.hits})
        raise SourceChanged("资料已更新或不可访问，请重新提问")

    log.info(
        "qa_answered",
        extra={
            "hits": found.hits,
            "citations": len(citations),
            "warnings": len(warnings),
            "model_revision": generated.model_revision,
            "context_messages": len(window),
            "query_rewritten": query.rewritten,
        },
    )
    result = Answer(
        status=AnswerStatus.ANSWERED.value,
        text=body,
        citations=citations,
        warnings=warnings,
        hits=found.hits,
        degraded=found.degraded,
        context_messages=len(window),
        query_rewritten=query.rewritten,
    )
    persist(session, actor, conversation_id, question, result, generated.model_revision)
    return result


def persist(
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
