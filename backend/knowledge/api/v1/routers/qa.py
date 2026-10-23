"""问答与会话接口。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from knowledge.api.deps import (
    get_actor,
    get_embedder,
    get_generator,
    get_session,
    get_settings_dep,
    request_id,
)
from knowledge.api.v1.schemas.common import Envelope, envelope
from knowledge.api.v1.schemas.requests import AskRequest, CreateConversationRequest
from knowledge.api.v1.schemas.responses import AnswerOut, CitationOut
from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.domain.errors import NotImplementedYet
from knowledge.domain.ports import Embedder, Generator
from knowledge.repositories import conversations as conversation_repo
from knowledge.services import qa

router = APIRouter(tags=["qa"])


@router.post("/qa", response_model=Envelope)
def ask(
    payload: AskRequest,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
    embedder: Embedder = Depends(get_embedder),
    generator: Generator = Depends(get_generator),
) -> dict:
    if payload.stream:
        raise NotImplementedYet("当前版本不支持流式输出")

    result = qa.answer(
        session,
        actor,
        settings,
        embedder,
        generator,
        question=payload.question,
        kb_ids=payload.knowledge_base_ids,
        top_k=payload.top_k,
        conversation_id=payload.conversation_id,
    )

    body = AnswerOut(
        answer_status=result.status,
        answer=result.text,
        citations=[CitationOut(**citation.to_dict()) for citation in result.citations],
        retrieval={"mode": result.retrieval_mode, "hits": result.hits, "degraded": result.degraded},
        warnings=result.warnings,
    )
    return envelope(body.model_dump(mode="json"), request_id(request))


@router.post("/conversations", status_code=201, response_model=Envelope)
def create_conversation(
    payload: CreateConversationRequest,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    conversation = conversation_repo.create(session, actor, payload.title)
    session.commit()
    return envelope(
        {"id": str(conversation.id), "title": conversation.title}, request_id(request)
    )
