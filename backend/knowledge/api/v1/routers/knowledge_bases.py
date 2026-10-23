"""知识库接口。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from knowledge.api.deps import get_actor, get_session, get_settings_dep, request_id
from knowledge.api.v1.schemas.common import Envelope, envelope
from knowledge.api.v1.schemas.requests import CreateKnowledgeBaseRequest
from knowledge.api.v1.schemas.responses import KnowledgeBaseOut
from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.services import knowledge as knowledge_service

router = APIRouter(prefix="/knowledge-bases", tags=["knowledge-bases"])


def _out(kb) -> dict:
    return KnowledgeBaseOut(
        id=kb.id, name=kb.name, status=kb.status.value, created_at=kb.created_at.isoformat()
    ).model_dump(mode="json")


@router.post("", status_code=201, response_model=Envelope)
def create(
    payload: CreateKnowledgeBaseRequest,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    kb = knowledge_service.create(session, actor, payload.name)
    return envelope(_out(kb), request_id(request))


@router.get("", response_model=Envelope)
def list_all(
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    items = [_out(kb) for kb in knowledge_service.list_owned(session, actor)]
    return envelope({"items": items}, request_id(request))


@router.delete("/{kb_id}", status_code=202, response_model=Envelope)
def delete(
    kb_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    cleanup_jobs = knowledge_service.delete(session, actor, kb_id, settings.max_attempts)
    return envelope(
        {"id": str(kb_id), "status": "deleting", "cleanup_jobs": cleanup_jobs},
        request_id(request),
    )
