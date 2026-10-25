import os
import uuid

import pytest
from sqlalchemy import select, text
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import Session

from knowledge.adapters.embedding import HashEmbedder
from knowledge.domain.entities import BuildState, JobState
from knowledge.domain.errors import ModelUnavailable, StaleJob
from knowledge.repositories import builds as build_repo
from knowledge.repositories import jobs as job_repo
from knowledge.repositories.tables import Chunk, Document, IndexBuild, Job
from knowledge.services import document, ingest, knowledge, retrieval
from knowledge.worker.loop import process_next

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated DB required"),
]


def upload(session, actor, settings, deps):
    kb = knowledge.create(session, actor, "audit")
    result = document.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="audit.txt",
        data=b"meeting room test policy",
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    return kb, result


def expire(session, job_id):
    session.execute(
        text(
            "UPDATE jobs SET lease_expires_at = clock_timestamp() - interval '1 second' WHERE id=:id"
        ),
        {"id": job_id},
    )
    session.commit()


def test_recovery_after_startup_creates_new_build(session, actor, settings, deps):
    _, result = upload(session, actor, settings, deps)
    token = uuid.uuid4()
    job_repo.claim_next(session, token, 120)
    assert job_repo.recover_expired(session) == 0
    expire(session, result.job_id)
    assert job_repo.recover_expired(session) == 1
    job = session.get(Job, result.job_id, populate_existing=True)
    assert job.build_id != result.build_id
    assert (
        session.get(IndexBuild, result.build_id, populate_existing=True).state == BuildState.FAILED
    )
    assert process_next(session, deps)
    session.expire_all()
    assert session.get(Job, result.job_id).state == JobState.SUCCEEDED
    assert session.get(Document, result.document_id).active_build_id == job.build_id


def test_expired_last_attempt_fails(session, actor, settings, deps):
    _, result = upload(session, actor, settings, deps)
    for _ in range(settings.max_attempts):
        assert job_repo.claim_next(session, uuid.uuid4(), 120)
        expire(session, result.job_id)
        assert job_repo.recover_expired(session) == 1
    assert job_repo.claim_next(session, uuid.uuid4(), 120) is None
    assert session.get(Job, result.job_id, populate_existing=True).state == JobState.FAILED


def test_old_task_never_adopts_new_version(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    new = document.add_version(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        document_id=old.document_id,
        filename="audit.txt",
        data=b"replacement policy",
        expected_generation=1,
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps)
    assert session.get(Job, old.job_id, populate_existing=True).state == JobState.CANCELLED
    assert session.get(Document, old.document_id, populate_existing=True).active_build_id is None
    assert process_next(session, deps)
    assert session.get(Job, new.job_id, populate_existing=True).state == JobState.SUCCEEDED
    assert (
        session.get(Document, old.document_id, populate_existing=True).active_build_id
        == new.build_id
    )


def test_expired_or_replaced_token_cannot_write(session, actor, settings, deps):
    _, result = upload(session, actor, settings, deps)
    token = uuid.uuid4()
    job_repo.claim_next(session, token, 120)
    expire(session, result.job_id)
    assert not job_repo.heartbeat(session, result.job_id, token, 120)
    with pytest.raises(StaleJob):
        build_repo.lock_execution(session, result.job_id, token)
    session.rollback()
    job_repo.recover_expired(session)
    job_repo.claim_next(session, uuid.uuid4(), 120)
    with pytest.raises(StaleJob):
        build_repo.lock_execution(session, result.job_id, token)
    session.rollback()


def test_publish_rollback_is_atomic(session, actor, settings, deps, monkeypatch):
    _, result = upload(session, actor, settings, deps)
    token = uuid.uuid4()
    job_repo.claim_next(session, token, 120)
    original = build_repo.publish

    def fail_after_changes(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("injected failure before commit")

    monkeypatch.setattr(build_repo, "publish", fail_after_changes)
    with pytest.raises(RuntimeError):
        ingest.run(
            session,
            job_id=result.job_id,
            lease_token=token,
            document_id=result.document_id,
            settings=settings,
            storage=deps.storage,
            registry=deps.registry,
            embedder=deps.embedder,
        )
    with Session(session.get_bind()) as observer:
        assert observer.get(Document, result.document_id).active_build_id is None
        assert observer.get(Job, result.job_id).state == JobState.RUNNING
        assert not list(observer.scalars(select(Chunk).where(Chunk.build_id == result.build_id)))


def test_model_switch_rejected_before_embedding(session, actor, settings, deps):
    kb, _ = upload(session, actor, settings, deps)
    assert process_next(session, deps)

    class WrongModel(HashEmbedder):
        def embed(self, texts):
            pytest.fail("incompatible query must not call model")

    wrong = WrongModel(model="other-model")
    with pytest.raises(ModelUnavailable):
        retrieval.search(session, actor, settings, wrong, query="meeting", kb_ids=[kb.id])
    assert retrieval.search(
        session,
        actor,
        settings,
        wrong,
        query="meeting room test policy",
        kb_ids=[kb.id],
        mode="keyword",
    )


def test_other_model_cannot_write_same_vector_index(session, actor, settings, deps):
    kb, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    wrong = HashEmbedder(model="other-model")
    new = document.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="other.txt",
        data=b"different-model data",
        embedder_model=wrong.model,
        embedder_dim=wrong.dimension,
    )
    token = uuid.uuid4()
    job_repo.claim_next(session, token, 120)
    with pytest.raises(ModelUnavailable):
        ingest.run(
            session,
            job_id=new.job_id,
            lease_token=token,
            document_id=new.document_id,
            settings=settings,
            storage=deps.storage,
            registry=deps.registry,
            embedder=wrong,
        )
    session.rollback()
    assert not list(session.scalars(select(Chunk).where(Chunk.build_id == new.build_id)))
    assert session.get(IndexBuild, old.build_id, populate_existing=True).state == BuildState.READY


def test_ready_chunks_unchanged_by_replayed_task(session, actor, settings, deps):
    _, result = upload(session, actor, settings, deps)
    token = uuid.uuid4()
    job_repo.claim_next(session, token, 120)
    kwargs = {
        "job_id": result.job_id,
        "lease_token": token,
        "document_id": result.document_id,
        "settings": settings,
        "storage": deps.storage,
        "registry": deps.registry,
        "embedder": deps.embedder,
    }
    ingest.run(session, **kwargs)
    original = list(
        session.execute(select(Chunk.id, Chunk.text).where(Chunk.build_id == result.build_id))
    )
    ingest.run(session, **kwargs)
    assert (
        list(session.execute(select(Chunk.id, Chunk.text).where(Chunk.build_id == result.build_id)))
        == original
    )
    assert session.get(Job, result.job_id, populate_existing=True).state == JobState.SUCCEEDED


def test_execution_lock_prevents_concurrent_takeover(session, actor, settings, deps):
    _, result = upload(session, actor, settings, deps)
    token = uuid.uuid4()
    job_repo.claim_next(session, token, 120)
    build_repo.lock_execution(session, result.job_id, token)
    with Session(session.get_bind()) as contender:
        contender.execute(text("SET LOCAL lock_timeout = '100ms'"))
        with pytest.raises(OperationalError):
            contender.execute(
                text("UPDATE jobs SET lease_token=:token WHERE id=:id"),
                {"token": uuid.uuid4(), "id": result.job_id},
            )
        contender.rollback()
    session.rollback()


def test_update_while_old_task_embeds_cannot_publish(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    token = uuid.uuid4()
    job_repo.claim_next(session, token, 120)
    new_results = []

    class UpdatingEmbedder(HashEmbedder):
        def embed(self, texts):
            with Session(session.get_bind(), expire_on_commit=False) as other:
                new_results.append(
                    document.add_version(
                        other,
                        actor,
                        settings,
                        deps.storage,
                        deps.registry,
                        document_id=old.document_id,
                        filename="audit.txt",
                        data=b"new policy",
                        expected_generation=1,
                        embedder_model=self.model,
                        embedder_dim=self.dimension,
                    )
                )
            return super().embed(texts)

    with pytest.raises(StaleJob):
        ingest.run(
            session,
            job_id=old.job_id,
            lease_token=token,
            document_id=old.document_id,
            settings=settings,
            storage=deps.storage,
            registry=deps.registry,
            embedder=UpdatingEmbedder(),
        )
    session.rollback()
    job_repo.finish(session, old.job_id, token, JobState.CANCELLED)
    assert process_next(session, deps)
    session.expire_all()
    assert session.get(Document, old.document_id).active_build_id == new_results[0].build_id
    assert not list(session.scalars(select(Chunk).where(Chunk.build_id == old.build_id)))


def test_unexpected_worker_error_is_sanitized(session, actor, settings, deps, monkeypatch, caplog):
    _, result = upload(session, actor, settings, deps)
    secret = "SELECT secret FROM private /private/storage/key test-token"

    def fail(*args, **kwargs):
        raise OSError(secret)

    monkeypatch.setattr(ingest, "run", fail)
    assert process_next(session, deps)
    job = session.get(Job, result.job_id, populate_existing=True)
    assert secret not in str(job.last_error)
    assert secret not in caplog.text
    assert job.last_error["code"] == "INTERNAL_ERROR"
    # Legacy rows must also be scrubbed at every response boundary.
    job.last_error = {"code": "INTERNAL_ERROR", "message": secret, "retryable": True}
    session.commit()
    from starlette.requests import Request

    from knowledge.api.v1.routers.jobs import get_job
    from knowledge.mcp.tools import job_status

    request = Request({"type": "http"})
    request.state.request_id = "audit"
    for response in (
        document.detail(session, actor, result.document_id),
        job_status(session, actor, result.job_id),
        get_job(result.job_id, request, session, actor),
    ):
        assert secret not in str(response)
