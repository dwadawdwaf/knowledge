"""任务数据访问：领取、续租、结束与恢复。"""

from __future__ import annotations

import json
import uuid
from typing import Any, cast

from sqlalchemy import Result, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, JobKind, JobState, Lifecycle
from knowledge.domain.errors import NotFound
from knowledge.repositories.tables import Document, Job, KnowledgeBase


def _affected(result: Result[Any]) -> int:
    # text() 更新在运行期返回 CursorResult；Result 类型上没有 rowcount
    return cast(CursorResult[Any], result).rowcount


def _error_param(error: dict | None) -> str | None:
    # text() SQL 里经 CAST(:error AS jsonb) 写入，须传 JSON 字符串
    return json.dumps(error, ensure_ascii=False) if error else None

_CLAIM = text(
    """
    UPDATE jobs SET state = 'running',
           attempt = attempt + 1,
           lease_token = :token,
           lease_expires_at = now() + make_interval(secs => :lease),
           heartbeat_at = now(),
           stage = NULL
    WHERE id = (
        SELECT id FROM jobs
        WHERE state = 'queued' AND available_at <= now()
        ORDER BY available_at
        LIMIT 1
        FOR UPDATE SKIP LOCKED
    )
    RETURNING id, kind, resource_id, attempt, max_attempts,
              EXTRACT(EPOCH FROM (now() - created_at)) AS queued_seconds
    """
)

_HEARTBEAT = text(
    """
    UPDATE jobs SET heartbeat_at = now(),
           lease_expires_at = now() + make_interval(secs => :lease)
    WHERE id = :job_id AND lease_token = :token AND state = 'running'
    """
)

_FINISH = text(
    """
    UPDATE jobs SET state = :state, stage = :stage, last_error = CAST(:error AS jsonb),
           lease_token = NULL, lease_expires_at = NULL
    WHERE id = :job_id AND lease_token = :token
    """
)

_SHELVE = text(
    """
    UPDATE jobs SET state = 'queued', stage = NULL, last_error = CAST(:error AS jsonb),
           available_at = now() + make_interval(secs => :delay),
           lease_token = NULL, lease_expires_at = NULL
    WHERE id = :job_id AND lease_token = :token
    """
)

_RECOVER = text(
    """
    UPDATE jobs SET state = 'queued', lease_token = NULL, lease_expires_at = NULL, stage = NULL
    WHERE state = 'running' AND lease_expires_at < now()
    """
)


def create(session: Session, kind: JobKind, resource_id: uuid.UUID, max_attempts: int = 3) -> Job:
    job = Job(kind=kind, resource_id=resource_id, state=JobState.QUEUED, max_attempts=max_attempts)
    session.add(job)
    session.flush()
    return job


def claim_next(session: Session, token: uuid.UUID, lease_seconds: int) -> dict | None:
    row = session.execute(_CLAIM, {"token": token, "lease": lease_seconds}).mappings().first()
    session.commit()
    return dict(row) if row else None


def heartbeat(session: Session, job_id: uuid.UUID, token: uuid.UUID, lease_seconds: int) -> bool:
    result = session.execute(_HEARTBEAT, {"job_id": job_id, "token": token, "lease": lease_seconds})
    session.commit()
    return _affected(result) > 0


def finish(
    session: Session,
    job_id: uuid.UUID,
    token: uuid.UUID,
    state: JobState,
    stage: str | None = None,
    error: dict | None = None,
) -> bool:
    result = session.execute(
        _FINISH,
        {
            "job_id": job_id,
            "token": token,
            "state": state.value,
            "stage": stage,
            "error": _error_param(error),
        },
    )
    session.commit()
    return _affected(result) > 0


def shelve(
    session: Session, job_id: uuid.UUID, token: uuid.UUID, error: dict, delay_seconds: int
) -> bool:
    result = session.execute(
        _SHELVE,
        {
            "job_id": job_id,
            "token": token,
            "error": _error_param(error),
            "delay": delay_seconds,
        },
    )
    session.commit()
    return _affected(result) > 0


def recover_expired(session: Session) -> int:
    result = session.execute(_RECOVER)
    session.commit()
    return _affected(result)


def set_stage(session: Session, job_id: uuid.UUID, token: uuid.UUID, stage: str) -> bool:
    result = session.execute(
        text(
            "UPDATE jobs SET stage = :stage WHERE id = :job_id"
            " AND lease_token = :token AND state = 'running'"
        ),
        {"stage": stage, "job_id": job_id, "token": token},
    )
    session.commit()
    return _affected(result) > 0


def get_owned(session: Session, actor: Actor, job_id: uuid.UUID) -> Job:
    stmt = (
        select(Job)
        .join(Document, Document.id == Job.resource_id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .where(Job.id == job_id, KnowledgeBase.owner_id == actor.subject_id)
    )
    job = session.scalar(stmt)
    if job is None:
        raise NotFound("任务不存在")
    return job


def latest_for_document(session: Session, document_id: uuid.UUID) -> Job | None:
    stmt = (
        select(Job)
        .where(Job.resource_id == document_id)
        .order_by(Job.created_at.desc())
        .limit(1)
    )
    return session.scalar(stmt)


def cancel_open_for_document(session: Session, document_id: uuid.UUID) -> None:
    stmt = select(Job).where(
        Job.resource_id == document_id, Job.state.in_([JobState.QUEUED, JobState.RUNNING])
    )
    for job in session.scalars(stmt):
        job.state = JobState.CANCELLED
        job.lease_token = None
        job.lease_expires_at = None
    session.flush()


def count_open_cleanup(session: Session, document_id: uuid.UUID) -> int:
    stmt = (
        select(Job.id)
        .where(
            Job.resource_id == document_id,
            Job.kind == JobKind.CLEANUP,
            Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
        )
        .limit(1)
    )
    return 1 if session.scalar(stmt) else 0


def doc_status_map(session: Session, document_ids: list[uuid.UUID]) -> dict[uuid.UUID, Lifecycle]:
    if not document_ids:
        return {}
    stmt = select(Document.id, Document.status).where(Document.id.in_(document_ids))
    return {row[0]: row[1] for row in session.execute(stmt)}
