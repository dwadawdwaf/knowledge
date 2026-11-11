"""提交事件只通知所属用户，回滚和任务心跳不制造刷新。"""

from __future__ import annotations

import hashlib
import importlib
import os
import uuid

import psycopg
import pytest
from psycopg import sql
from sqlalchemy import text

from knowledge.domain.entities import Actor, JobKind
from knowledge.repositories import jobs
from knowledge.services import knowledge

pytestmark = [pytest.mark.integration, pytest.mark.skipif(
    not os.getenv("KS_TEST_DATABASE_URL"), reason="isolated PostgreSQL required",
)]


def listen(settings, subject: str):
    connection = psycopg.connect(
        settings.database_url.replace("postgresql+psycopg://", "postgresql://"), autocommit=True
    )
    channel = "ks_changes_" + hashlib.md5(subject.encode(), usedforsecurity=False).hexdigest()
    connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))
    return connection


def notifications(connection, timeout: float = 0.2):
    return list(connection.notifies(timeout=timeout, stop_after=1))


def test_commit_owner_isolation_rollback_and_heartbeat(session, settings, deps):
    migration = importlib.import_module("migrations.versions.0008_document_events")
    session.execute(text(migration.INSTALL_SQL))
    session.commit()
    actor = Actor("event-owner", "default")
    with listen(settings, actor.subject_id) as own, listen(settings, "event-other") as other:
        kb = knowledge.create(session, actor, "自制事件库")
        notice = notifications(own)
        assert len(notice) == 1 and notice[0].payload == ""
        assert notifications(other) == []

        session.execute(text("UPDATE knowledge_bases SET name='回滚' WHERE id=:id"), {"id": kb.id})
        session.rollback()
        assert notifications(own) == []

        from knowledge.services import document

        ack = document.upload(
            session, actor, settings, deps.storage, deps.registry,
            kb_id=kb.id, filename="synthetic.txt", data="自制事件样本".encode(),
            embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
        )
        assert len(notifications(own)) == 1
        session.execute(text("UPDATE jobs SET heartbeat_at=now() WHERE id=:id"), {"id": ack.job_id})
        session.commit()
        assert notifications(own) == []
        session.execute(text("UPDATE jobs SET stage='parsing' WHERE id=:id"), {"id": ack.job_id})
        session.commit()
        assert len(notifications(own)) == 1
        assert notifications(other) == []

        document.delete(session, actor, ack.document_id, settings.max_attempts)
        assert len(notifications(own)) == 1
        from knowledge.domain.errors import NotFound

        with pytest.raises(NotFound):
            document.detail(session, actor, ack.document_id)


def test_detached_jobs_do_not_break_notifications(session):
    migration = importlib.import_module("migrations.versions.0008_document_events")
    session.execute(text(migration.INSTALL_SQL))
    jobs.create(session, JobKind.CLEANUP, uuid.uuid4(), 3)
    session.commit()
