"""任务状态接口。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from knowledge.api.deps import get_actor, get_session, request_id
from knowledge.api.v1.schemas.common import Envelope, envelope
from knowledge.domain.entities import Actor
from knowledge.repositories import jobs as job_repo

router = APIRouter(tags=["jobs"])


@router.get("/jobs/{job_id}", response_model=Envelope)
def get_job(
    job_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    job = job_repo.get_owned(session, actor, job_id)
    return envelope(
        {
            "id": str(job.id),
            "kind": job.kind.value,
            "state": job.state.value,
            "stage": job.stage,
            "attempt": job.attempt,
            "max_attempts": job.max_attempts,
            "progress": {"stage": job.stage, "processed_items": None, "total_items": None},
            "last_error": job.last_error,
            "created_at": job.created_at.isoformat() if job.created_at else None,
        },
        request_id(request),
    )
