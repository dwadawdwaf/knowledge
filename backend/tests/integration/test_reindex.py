"""同模型重建：复用原件、恢复终态失败、授权与并发栅栏。"""

import os
import uuid
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from threading import Barrier

import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, BuildState, JobKind, JobState
from knowledge.domain.errors import ModelUnavailable, NotFound, VersionConflict
from knowledge.repositories import reindex as reindex_repo
from knowledge.repositories.tables import Chunk, Document, DocumentVersion, IndexBuild, Job
from knowledge.services import document, knowledge, retrieval
from knowledge.worker.loop import process_next

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated DB required"),
]


def upload(session, actor, settings, deps):
    kb = knowledge.create(session, actor, "重建测试")
    result = document.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="room.txt",
        data=b"meeting room policy",
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    return kb, result


def reindex(session, actor, settings, deps, result, generation=1, **overrides):
    arguments = {
        "document_id": result.document_id,
        "version_id": result.version_id,
        "expected_generation": generation,
        "embedder_model": deps.embedder.model,
        "embedder_dim": deps.embedder.dimension,
    }
    arguments.update(overrides)
    return document.reindex(session, actor, settings, **arguments)


def test_reindex_preserves_original_until_atomic_publication(session, actor, settings, deps):
    kb, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    before = [
        (c.id, c.text, c.locator)
        for c in session.scalars(select(Chunk).where(Chunk.build_id == old.build_id))
    ]
    version = session.get(DocumentVersion, old.version_id)
    original = (version.object_key, version.sha256, deps.storage.get(version.object_key))
    changed_settings = settings.model_copy(
        update={"chunk_max_chars": settings.chunk_max_chars + 17}
    )
    new = reindex(session, actor, changed_settings, deps, old)
    assert new.generation == 2 and new.version_id == old.version_id
    assert new.build_id != old.build_id and new.job_id != old.job_id
    assert session.get(Job, new.job_id).kind == JobKind.REINDEX
    assert session.get(Job, new.job_id).attempt == 0
    assert (
        session.get(IndexBuild, new.build_id).chunk_config
        == session.get(IndexBuild, old.build_id).chunk_config
    )
    assert session.get(Document, old.document_id).active_build_id == old.build_id
    assert {
        hit.build_id
        for hit in retrieval.search(
            session, actor, settings, deps.embedder, query="meeting room policy", kb_ids=[kb.id]
        )
    } == {str(old.build_id)}
    assert process_next(session, deps)
    session.expire_all()
    assert session.get(Document, old.document_id).active_build_id == new.build_id
    assert session.get(Document, old.document_id).current_version_id == old.version_id
    assert session.get(IndexBuild, old.build_id).state == BuildState.RETIRED
    assert session.get(IndexBuild, new.build_id).state == BuildState.READY
    assert [
        (c.id, c.text, c.locator)
        for c in session.scalars(select(Chunk).where(Chunk.build_id == old.build_id))
    ] == before
    assert session.scalar(select(func.count()).select_from(DocumentVersion)) == 1
    assert (version.object_key, version.sha256, deps.storage.get(version.object_key)) == original


def test_failed_job_can_recover_without_new_version(session, actor, settings, deps):
    settings = settings.model_copy(update={"max_attempts": 1})
    deps = replace(deps, settings=settings)
    _, old = upload(session, actor, settings, deps)

    class FailingEmbedder:
        model = deps.embedder.model
        dimension = deps.embedder.dimension

        def embed(self, texts):
            raise ModelUnavailable("自制故障")

    assert process_next(session, replace(deps, embedder=FailingEmbedder()))
    assert session.get(Job, old.job_id, populate_existing=True).state == JobState.FAILED
    new = reindex(session, actor, settings, deps, old)
    assert process_next(session, deps)
    session.expire_all()
    assert session.get(Job, old.job_id).state == JobState.FAILED
    assert session.get(IndexBuild, old.build_id).state == BuildState.FAILED
    assert session.get(Job, new.job_id).state == JobState.SUCCEEDED
    assert session.get(Document, old.document_id).current_version_id == old.version_id


def test_rechunk_uses_current_version_and_preserves_history(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    historical = session.get(IndexBuild, old.build_id)
    old_config = {**historical.chunk_config, "strategy": "legacy-v1", "parser_profile": "deepdoc-v0.22.0-v1"}
    historical.chunk_config = old_config
    doc = session.get(Document, old.document_id)
    newer = document.add_version(
        session, actor, settings, deps.storage, deps.registry, document_id=doc.id,
        filename="room.txt", data=b"new policy", expected_generation=doc.generation,
        embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
    )
    # 模拟更新失败；维护重切不应将该版本发布。
    session.get(Job, newer.job_id).state = JobState.FAILED
    session.get(IndexBuild, newer.build_id).state = BuildState.FAILED
    session.commit()
    kwargs = {
        "document_id": doc.id, "embedder_model": deps.embedder.model, "embedder_dim": deps.embedder.dimension,
    }
    stranger = Actor("other", actor.tenant_id)
    assert reindex_repo.owned_document_ids(session, stranger) == []
    with pytest.raises(NotFound):
        document.rechunk_existing(session, stranger, settings, **kwargs)
    session.rollback()
    ack = document.rechunk_existing(session, actor, settings, **kwargs)
    assert ack and ack.version_id == old.version_id and ack.build_id != old.build_id
    assert session.get(Document, old.document_id).active_build_id == old.build_id
    assert process_next(session, deps, target_job_id=ack.job_id)
    session.expire_all()
    assert session.get(Document, old.document_id).active_build_id == ack.build_id
    assert session.get(Document, old.document_id).current_version_id == old.version_id
    assert session.get(IndexBuild, old.build_id).chunk_config == old_config
    published = session.get(IndexBuild, ack.build_id)
    assert published.chunk_config["strategy"] == "evidence-unit-v1"
    assert published.chunk_config["parser_profile"] == "legacy-v1"
    assert "historical_text_parser_replaced" in published.warnings
    assert document.rechunk_existing(session, actor, settings, **kwargs) is None
    session.rollback()
    document.delete(session, actor, old.document_id, settings.max_attempts)
    with pytest.raises(NotFound):
        document.rechunk_existing(session, actor, settings, **kwargs)


def test_queued_legacy_build_is_replaced_without_editing_frozen_config(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    queued = session.get(IndexBuild, old.build_id)
    legacy_config = {k: v for k, v in queued.chunk_config.items() if k != "strategy"}
    queued.chunk_config = legacy_config
    session.commit()
    assert process_next(session, deps, target_job_id=old.job_id)
    session.expire_all()
    assert session.get(IndexBuild, old.build_id).state == BuildState.CANCELLED
    assert session.get(IndexBuild, old.build_id).chunk_config == legacy_config
    active = session.get(Document, old.document_id).active_build_id
    assert active != old.build_id
    assert session.get(Job, old.job_id).build_id == active
    assert session.get(IndexBuild, active).chunk_config == {**legacy_config, "strategy": "evidence-unit-v1"}


@pytest.mark.parametrize("running", [False, True])
def test_open_job_rejects_even_fresh_generation(session, actor, settings, deps, running):
    _, old = upload(session, actor, settings, deps)
    if running:
        from knowledge.repositories import jobs

        assert jobs.claim_next(session, uuid.uuid4(), 120)
    with pytest.raises(VersionConflict):
        reindex(session, actor, settings, deps, old)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Job)) == 1
    assert session.scalar(select(func.count()).select_from(IndexBuild)) == 1


@pytest.mark.parametrize(
    "overrides",
    [
        {"expected_generation": 0},
        {"embedder_model": "different"},
        {"embedder_dim": 512},
    ],
)
def test_conflict_makes_no_new_rows(session, actor, settings, deps, overrides):
    _, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    with pytest.raises(VersionConflict):
        reindex(session, actor, settings, deps, old, **overrides)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(Job)) == 1
    assert session.get(Document, old.document_id).generation == 1


def test_other_owner_and_deleted_resource_not_found(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    with pytest.raises(NotFound):
        reindex(session, Actor(subject_id="other", tenant_id="default"), settings, deps, old)
    session.rollback()
    document.delete(session, actor, old.document_id, settings.max_attempts)
    with pytest.raises(NotFound):
        reindex(session, actor, settings, deps, old)
    session.rollback()
    assert process_next(session, deps)
    assert not process_next(session, deps)
    with pytest.raises(NotFound):
        reindex(session, actor, settings, deps, old)


def test_rejects_old_or_foreign_version(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    newer = document.add_version(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        document_id=old.document_id,
        filename="room.txt",
        data=b"new meeting policy",
        expected_generation=1,
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps)
    with pytest.raises(VersionConflict):
        reindex(session, actor, settings, deps, old, generation=2)
    session.rollback()
    with pytest.raises(NotFound):
        reindex(session, actor, settings, deps, newer, generation=2, version_id=uuid.uuid4())
    session.rollback()
    _, foreign = upload(session, actor, settings, deps)
    with pytest.raises(NotFound):
        reindex(session, actor, settings, deps, newer, generation=2, version_id=foreign.version_id)


def test_concurrent_duplicate_creates_only_one_job(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    barrier = Barrier(2)

    def submit():
        with Session(session.get_bind(), expire_on_commit=False) as contender:
            # 故意预载旧对象，确认加锁查询会刷新 identity map 中的代次。
            cached = contender.get(Document, old.document_id)
            assert cached.generation == 1
            barrier.wait(timeout=10)
            try:
                reindex(contender, actor, settings, deps, old)
                return "accepted"
            except VersionConflict:
                contender.rollback()
                return "conflict"

    session.rollback()
    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(lambda _: submit(), range(2)))
    assert sorted(results) == ["accepted", "conflict"]
    assert session.scalar(select(func.count()).select_from(Job)) == 2


def test_library_deletion_blocks_reindex_before_cleanup(session, actor, settings, deps):
    kb, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    knowledge.delete(session, actor, kb.id, settings.max_attempts)
    with pytest.raises(NotFound):
        reindex(session, actor, settings, deps, old)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(IndexBuild)) == 1


def test_failed_rebuild_keeps_published_content(session, actor, settings, deps):
    kb, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    new = reindex(session, actor, settings.model_copy(update={"max_attempts": 1}), deps, old)

    class FailingEmbedder:
        model = deps.embedder.model
        dimension = deps.embedder.dimension

        def embed(self, texts):
            raise ModelUnavailable("自制故障")

    assert process_next(session, replace(deps, embedder=FailingEmbedder()))
    session.expire_all()
    assert session.get(Job, new.job_id).state == JobState.FAILED
    assert session.get(Document, old.document_id).active_build_id == old.build_id
    assert session.get(IndexBuild, old.build_id).state == BuildState.READY
    assert {
        hit.build_id
        for hit in retrieval.search(
            session, actor, settings, deps.embedder, query="meeting room policy", kb_ids=[kb.id]
        )
    } == {str(old.build_id)}


def test_failed_latest_version_recovery_not_old_published_version(session, actor, settings, deps):
    _, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    newest = document.add_version(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        document_id=old.document_id,
        filename="room.txt",
        data=b"latest policy",
        expected_generation=1,
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    from knowledge.repositories import jobs

    token = uuid.uuid4()
    assert jobs.claim_next(session, token, 120)
    assert jobs.finish(session, newest.job_id, token, JobState.FAILED)
    new = reindex(session, actor, settings, deps, newest, generation=2)
    assert session.get(Document, old.document_id).current_version_id == old.version_id
    assert process_next(session, deps)
    session.expire_all()
    assert session.get(Document, old.document_id).current_version_id == newest.version_id
    assert session.get(Document, old.document_id).active_build_id == new.build_id


def test_enqueuing_rollback_leaves_no_partial_build_or_generation(
    session, actor, settings, deps, monkeypatch
):
    _, old = upload(session, actor, settings, deps)
    assert process_next(session, deps)
    from knowledge.repositories import reindex as repository

    original = repository.enqueue

    def interrupted(*args, **kwargs):
        original(*args, **kwargs)
        raise RuntimeError("自制事务故障")

    monkeypatch.setattr(repository, "enqueue", interrupted)
    with pytest.raises(RuntimeError, match="自制事务故障"):
        reindex(session, actor, settings, deps, old)
    session.rollback()
    assert session.scalar(select(func.count()).select_from(IndexBuild)) == 1
    assert session.scalar(select(func.count()).select_from(Job)) == 1
    assert session.get(Document, old.document_id).generation == 1
