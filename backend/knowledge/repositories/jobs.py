"""任务数据访问：领取、续租、结束与恢复。"""

from __future__ import annotations

import uuid
from datetime import timedelta
from typing import Any, cast

from sqlalchemy import Result, func, select, text
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, BuildState, JobKind, JobState, Lifecycle
from knowledge.domain.errors import NotFound, public_job_error
from knowledge.repositories import builds as build_repo
from knowledge.repositories.tables import Document, IndexBuild, Job, KnowledgeBase


def _affected(result: Result[Any]) -> int:
    # text() 更新在运行期返回 CursorResult；Result 类型上没有 rowcount
    return cast(CursorResult[Any], result).rowcount


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
        WHERE state = 'queued' AND available_at <= now() AND attempt < max_attempts
          AND (CAST(:job_id AS uuid) IS NULL OR id = :job_id)
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
      AND lease_expires_at > clock_timestamp()
    """
)


def create(
    session: Session,
    kind: JobKind,
    resource_id: uuid.UUID,
    max_attempts: int = 3,
    *,
    build_id: uuid.UUID | None = None,
    generation: int | None = None,
) -> Job:
    job = Job(
        kind=kind,
        resource_id=resource_id,
        state=JobState.QUEUED,
        max_attempts=max_attempts,
        build_id=build_id,
        generation=generation,
    )
    session.add(job)
    session.flush()
    return job


def claim_next(
    session: Session, token: uuid.UUID, lease_seconds: int, *, job_id: uuid.UUID | None = None,
) -> dict | None:
    row = session.execute(_CLAIM, {"token": token, "lease": lease_seconds, "job_id": job_id}).mappings().first()
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
    _, job = build_repo.lock_job(session, job_id)
    now = session.execute(select(func.clock_timestamp())).scalar_one()
    if (
        job is None
        or job.lease_token != token
        or job.state != JobState.RUNNING
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        session.rollback()
        return False
    _terminal_build(session, job, state)
    job.state, job.stage, job.last_error = state, stage, public_job_error(error)
    job.lease_token = None
    job.lease_expires_at = None
    session.commit()
    return True


def shelve(
    session: Session, job_id: uuid.UUID, token: uuid.UUID, error: dict, delay_seconds: int
) -> bool:
    document, job = build_repo.lock_job(session, job_id)
    now = session.execute(select(func.clock_timestamp())).scalar_one()
    if (
        job is None
        or job.lease_token != token
        or job.state != JobState.RUNNING
        or job.lease_expires_at is None
        or job.lease_expires_at <= now
    ):
        session.rollback()
        return False
    _requeue(session, document, job, error, delay_seconds)
    session.commit()
    return True


def recover_expired(session: Session) -> int:
    ids = list(
        session.scalars(
            select(Job.id).where(
                Job.state == JobState.RUNNING, Job.lease_expires_at <= func.clock_timestamp()
            )
        )
    )
    count = 0
    for job_id in ids:
        document, job = build_repo.lock_job(session, job_id)
        now = session.execute(select(func.clock_timestamp())).scalar_one()
        if (
            job is not None
            and job.state == JobState.RUNNING
            and job.lease_expires_at is not None
            and job.lease_expires_at <= now
        ):
            _requeue(session, document, job, {"code": "STALE_JOB", "retryable": True}, 0)
            count += 1
        session.commit()
    session.commit()
    return count


def _terminal_build(session: Session, job: Job, state: JobState) -> None:
    if job.build_id is None or state not in (JobState.FAILED, JobState.CANCELLED):
        return
    build = session.get(IndexBuild, job.build_id)
    if build is not None and build.state in (BuildState.QUEUED, BuildState.RUNNING):
        build.state = BuildState.FAILED if state == JobState.FAILED else BuildState.CANCELLED
    _release_pending(session, job)
    session.flush()


def _release_pending(session: Session, job: Job) -> None:
    """任务终态后不再占用文档的待发布指针，避免悬挂引用阻塞后续清理与重建。"""
    if job.build_id is None or job.resource_id is None:
        return
    document = session.get(Document, job.resource_id)
    if document is not None and document.pending_build_id == job.build_id:
        document.pending_build_id = None


def _requeue(
    session: Session, document: Document | None, job: Job, error: dict, delay_seconds: int
) -> None:
    state = JobState.QUEUED
    if job.kind != JobKind.CLEANUP and not build_repo.target_matches(document, job):
        state = JobState.CANCELLED
    elif job.attempt >= job.max_attempts:
        state = JobState.FAILED
    elif job.kind != JobKind.CLEANUP:
        assert document is not None
        if not build_repo.retry_build(session, document, job):
            state = JobState.CANCELLED
    _terminal_build(session, job, state)
    job.state = state
    job.stage = None
    job.last_error = public_job_error(error)
    job.lease_token = None
    job.lease_expires_at = None
    job.available_at = session.execute(select(func.clock_timestamp())).scalar_one() + timedelta(
        seconds=delay_seconds
    )


def set_stage(session: Session, job_id: uuid.UUID, token: uuid.UUID, stage: str) -> bool:
    result = session.execute(
        text(
            "UPDATE jobs SET stage = :stage WHERE id = :job_id"
            " AND lease_token = :token AND state = 'running'"
            " AND lease_expires_at > clock_timestamp()"
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
        select(Job).where(Job.resource_id == document_id).order_by(Job.created_at.desc()).limit(1)
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
        _terminal_build(session, job, JobState.CANCELLED)
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
