"""问答与会话接口。"""

from __future__ import annotations

import asyncio
import uuid
from collections.abc import AsyncIterator

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse
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
from knowledge.domain.errors import ValidationFailed
from knowledge.domain.ports import Embedder, Generator
from knowledge.repositories import conversations as conversation_repo
from knowledge.repositories import knowledge_bases as kb_repo
from knowledge.services import conversations as conversation_service
from knowledge.services import qa, qa_stream, retrieval

router = APIRouter(tags=["qa"])


def _stream_response(
    payload: AskRequest,
    actor: Actor,
    settings: Settings,
    embedder: Embedder,
    generator: Generator,
    session: Session,
) -> StreamingResponse:
    """建立 SSE 前先做参数与权限预检，失败时仍返回普通 JSON 错误信封。"""
    retrieval.validate_query(payload.question, settings)
    if not payload.knowledge_base_ids:
        raise ValidationFailed("knowledge_base_ids 不能为空")
    if len(payload.knowledge_base_ids) > settings.kb_scope_max:
        raise ValidationFailed(f"知识库范围最多 {settings.kb_scope_max} 个")
    kb_repo.get_active(session, actor, payload.knowledge_base_ids)
    if payload.conversation_id is not None:
        conversation_repo.get_owned(session, actor, payload.conversation_id)
    # 流内使用独立会话，预检结束即归还连接：否则一次长回答会占住池里的连接。
    session.close()

    run = qa_stream.StreamRun(
        actor=actor,
        settings=settings,
        embedder=embedder,
        generator=generator,
        question=payload.question,
        kb_ids=payload.knowledge_base_ids,
        top_k=payload.top_k,
        conversation_id=payload.conversation_id,
        document_ids=payload.document_ids,
    )

    async def encode() -> AsyncIterator[str]:
        run.start(asyncio.get_running_loop())
        async for chunk in run.events():
            yield chunk

    # 请求编号由请求中间件统一写到响应头，这里不再重复设置。
    return StreamingResponse(
        encode(), media_type="text/event-stream", headers=dict(qa_stream.SSE_HEADERS)
    )


@router.post("/qa", response_model=Envelope)
def ask(
    payload: AskRequest,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
    embedder: Embedder = Depends(get_embedder),
    generator: Generator = Depends(get_generator),
) -> dict | StreamingResponse:
    if payload.stream:
        return _stream_response(payload, actor, settings, embedder, generator, session)

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
        document_ids=payload.document_ids,
    )

    body = AnswerOut(
        answer_status=result.status,
        answer=result.text,
        citations=[CitationOut(**citation.to_dict()) for citation in result.citations],
        retrieval={
            "mode": result.retrieval_mode,
            "hits": result.hits,
            "degraded": result.degraded,
            "context_messages": result.context_messages,
            "query_rewritten": result.query_rewritten,
        },
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


@router.delete("/conversations/{conversation_id}", response_model=Envelope)
def delete_conversation(
    conversation_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    """删除会话与消息。回答问题本身不删，知识库资料不受影响。

    用 200 + envelope 而不是 204：前端统一按 JSON 解析响应，空体需要额外分支。
    """
    conversation_service.delete(session, actor, conversation_id)
    return envelope({"id": str(conversation_id), "deleted": True}, request_id(request))


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
