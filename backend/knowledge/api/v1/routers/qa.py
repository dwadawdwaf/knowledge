"""问答与会话接口。"""

from __future__ import annotations

import uuid

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
from knowledge.services import conversations as conversation_service
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
    conversation = conversation_service.create(session, actor, payload.title)
    return envelope(
        {"id": str(conversation.id), "title": conversation.title}, request_id(request)
    )


@router.get("/conversations", response_model=Envelope)
def list_conversations(
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    items = [
        {
            "id": str(conversation.id),
            "title": conversation.title,
            "created_at": conversation.created_at.isoformat(),
        }
        for conversation in conversation_service.list_owned(session, actor)
    ]
    return envelope({"items": items}, request_id(request))


@router.get("/conversations/{conversation_id}", response_model=Envelope)
def get_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    conversation, messages = conversation_service.detail(session, actor, conversation_id)
    return envelope(
        {
            "id": str(conversation.id),
            "title": conversation.title,
            "created_at": conversation.created_at.isoformat(),
            "messages": [
                {
                    "id": str(message.id),
                    "role": message.role,
                    "body": message.body,
                    "answer_status": message.answer_status,
                    "citations": message.citations or [],
                    "created_at": message.created_at.isoformat(),
                }
                for message in messages
            ],
        },
        request_id(request),
    )
