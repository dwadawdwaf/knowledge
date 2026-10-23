"""分块预览接口：反查文档、版本与原文位置。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from knowledge.api.deps import get_actor, get_session, request_id
from knowledge.api.v1.schemas.common import Envelope, envelope
from knowledge.domain.entities import Actor
from knowledge.repositories import chunks as chunk_repo

router = APIRouter(tags=["chunks"])


@router.get("/chunks/{chunk_id}", response_model=Envelope)
def get_chunk(
    chunk_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    return envelope(chunk_repo.get_owned(session, actor, chunk_id), request_id(request))
