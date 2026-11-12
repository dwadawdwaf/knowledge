"""任务执行：领取、续租、分发、失败归因。业务规则全部复用 services。"""

from __future__ import annotations

import threading
import time
import uuid
from dataclasses import dataclass
from typing import Self

from sqlalchemy.orm import Session

from knowledge.adapters.embedding import build_embedder
from knowledge.adapters.parser import build_parser_registry
from knowledge.adapters.storage import LocalStorage
from knowledge.config import Settings, get_settings
from knowledge.domain.entities import JobKind, JobState
from knowledge.domain.errors import DomainError, StaleJob, TransientError, public_job_error
from knowledge.domain.ports import Embedder, ObjectStorage
from knowledge.logging import get_logger
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.session import new_session
from knowledge.services import cleanup, ingest

NON_RETRYABLE = {
    "UNSUPPORTED_FORMAT",
    "EMPTY_CONTENT",
    "PASSWORD_PROTECTED",
    "PARSE_FAILED",
}
BACKOFF_BASE_SECONDS = 5

log = get_logger("worker")


@dataclass
class WorkerDeps:
    settings: Settings
    storage: ObjectStorage
    registry: object
    embedder: Embedder


def build_deps(settings: Settings | None = None) -> WorkerDeps:
    resolved = settings or get_settings()
    return WorkerDeps(
        settings=resolved,
        storage=LocalStorage(resolved.storage_root),
        registry=build_parser_registry(resolved),
        embedder=build_embedder(resolved),
    )


class _Heartbeat:
    """独立线程续租，避免解析或模型调用阻塞租约。"""

    def __init__(self, job_id: uuid.UUID, token: uuid.UUID, interval: int, lease: int) -> None:
        self._job_id = job_id
        self._token = token
        self._interval = max(5, interval)
        self._lease = lease
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True)

    def _run(self) -> None:
        while not self._stop.wait(self._interval):
            session = new_session()
            try:
                job_repo.heartbeat(session, self._job_id, self._token, self._lease)
            except Exception:  # noqa: BLE001 — 心跳失败不抛出，由主流程的租约校验兜底
                session.rollback()
            finally:
                session.close()

    def __enter__(self) -> Self:
        self._thread.start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self._stop.set()
        self._thread.join(timeout=2)


def process_next(session: Session, deps: WorkerDeps, *, target_job_id: uuid.UUID | None = None) -> bool:
    """领取并处理一个任务；无任务时返回 False。"""
    token = uuid.uuid4()
    job = job_repo.claim_next(session, token, deps.settings.lease_seconds, job_id=target_job_id)
    if job is None:
        return False

    job_id: uuid.UUID = job["id"]
    attempt = int(job["attempt"])
    max_attempts = int(job["max_attempts"])
    kind = job["kind"]
    resource_id: uuid.UUID = job["resource_id"]
    log.info(
        "job_claimed",
        extra={
            "job_id": str(job_id),
            "kind": kind,
            "attempt": attempt,
            "queue_seconds": round(float(job["queued_seconds"]), 3),
        },
    )

    started = time.monotonic()
    try:
        with _Heartbeat(
            job_id, token, deps.settings.heartbeat_seconds, deps.settings.lease_seconds
        ):
            if kind == JobKind.INGEST.value or kind == JobKind.REINDEX.value:
                ingest.run(
                    session,
                    job_id=job_id,
                    lease_token=token,
                    document_id=resource_id,
                    settings=deps.settings,
                    storage=deps.storage,
                    registry=deps.registry,
                    embedder=deps.embedder,
                )
            elif kind == JobKind.CLEANUP.value:
                cleanup.run(
                    session,
                    job_id=job_id,
                    lease_token=token,
                    document_id=resource_id,
                    storage=deps.storage,
                    settings=deps.settings,
                )
            else:
                job_repo.finish(
                    session,
                    job_id,
                    token,
                    JobState.CANCELLED,
                    None,
                    {"code": "UNKNOWN_KIND", "message": kind, "retryable": False},
                )
    except StaleJob:
        session.rollback()
        job_repo.finish(session, job_id, token, JobState.CANCELLED, "cancelled")
    except DomainError as exc:
        _handle_failure(session, job_id, token, resource_id, attempt, max_attempts, exc)
    except Exception:  # noqa: BLE001 — 原始异常可能含 SQL 参数和正文，不向外传播
        wrapped = TransientError("任务处理失败")
        _handle_failure(session, job_id, token, resource_id, attempt, max_attempts, wrapped)

    log.info(
        "job_finished",
        extra={
            "job_id": str(job_id),
            "kind": kind,
            "duration_ms": round((time.monotonic() - started) * 1000, 1),
        },
    )
    return True


def _handle_failure(
    session: Session,
    job_id: uuid.UUID,
    token: uuid.UUID,
    document_id: uuid.UUID,
    attempt: int,
    max_attempts: int,
    exc: DomainError,
) -> None:
    session.rollback()
    error = public_job_error({"code": exc.code, "message": exc.message, "retryable": exc.retryable})
    assert error is not None
    retryable = exc.retryable and exc.code not in NON_RETRYABLE

    if retryable and attempt < max_attempts:
        job_repo.shelve(
            session, job_id, token, error, BACKOFF_BASE_SECONDS * (2 ** max(0, attempt - 1))
        )
        log.warning(
            "job_retry_scheduled",
            extra={
                "job_id": str(job_id),
                "attempt": attempt,
                "max_attempts": max_attempts,
                "error_code": error["code"],
                "error_detail": exc.message,
            },
        )
        return

    job_repo.finish(session, job_id, token, JobState.FAILED, None, error)
    log.error(
        "job_failed",
        extra={
            "job_id": str(job_id),
            "attempt": attempt,
            "max_attempts": max_attempts,
            "error_code": error["code"],
            "error_detail": exc.message,
        },
    )


def run_forever(settings: Settings | None = None) -> None:
    resolved = settings or get_settings()
    deps = build_deps(resolved)

    session = new_session()
    try:
        recovered = job_repo.recover_expired(session)
        if recovered:
            log.warning("lease_recovered", extra={"count": recovered})
        reclaimed = cleanup.reclaim_orphans(session, deps.storage, resolved.orphan_max_age_seconds)
        if reclaimed:
            log.info("orphans_reclaimed", extra={"count": reclaimed})
    finally:
        session.close()

    while True:
        session = new_session()
        try:
            recovered = job_repo.recover_expired(session)
            if recovered:
                log.warning("lease_recovered", extra={"count": recovered})
            worked = process_next(session, deps)
        finally:
            session.close()
        if not worked:
            time.sleep(resolved.worker_poll_seconds)
