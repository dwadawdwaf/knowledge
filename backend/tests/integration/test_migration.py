import os
import pathlib
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from knowledge.config import Settings, get_settings
from knowledge.domain.entities import Actor
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.worker.loop import build_deps, process_next

URL = os.getenv("KS_TEST_MIGRATION_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="isolated migration DB required"),
]


def test_empty_and_populated_migration(monkeypatch, tmp_path):
    monkeypatch.setenv("KS_DATABASE_URL", URL)
    get_settings.cache_clear()
    cfg = Config()
    cfg.set_main_option(
        "script_location", str(pathlib.Path(__file__).resolve().parents[2] / "migrations")
    )
    engine = create_engine(URL)
    try:
        command.upgrade(cfg, "head")
        assert "ix_chunks_tsv_english_fts" in {i["name"] for i in inspect(engine).get_indexes("chunks")}
        assert "structure" in {c["name"] for c in inspect(engine).get_columns("chunks")}
        assert {"build_id", "generation"} <= {
            c["name"] for c in inspect(engine).get_columns("jobs")
        }
        command.downgrade(cfg, "0001_initial")
        job_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO jobs(id,kind,resource_id,state) VALUES (:id,'ingest',:doc,'queued')"
                ),
                {"id": job_id, "doc": uuid.uuid4()},
            )
        with pytest.raises(DBAPIError, match="Drain ingest/reindex"):
            command.upgrade(cfg, "head")
        assert "build_id" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE jobs SET state='succeeded' WHERE id=:id"), {"id": job_id}
            )
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert (
                connection.execute(
                    text("SELECT state FROM jobs WHERE id=:id"), {"id": job_id}
                ).scalar_one()
                == "succeeded"
            )
        settings = Settings.model_construct(database_url=URL, storage_root=str(tmp_path / "objects"))
        deps = build_deps(settings)
        actor = Actor("migration-owner", "default")
        with Session(engine, expire_on_commit=False) as session:
            kb = knowledge_service.create(session, actor, "迁移自制资料")
            document_service.upload(
                session, actor, settings, deps.storage, deps.registry, kb_id=kb.id,
                filename="synthetic.txt", data="迁移前后的片段原文保持一致。".encode(),
                embedder_model=deps.embedder.model, embedder_dim=deps.embedder.dimension,
            )
            while process_next(session, deps):
                pass
        with engine.connect() as connection:
            before = connection.execute(text("SELECT id,text,locator,embedding::text FROM chunks")).all()
            assert before
        command.downgrade(cfg, "0003_chunk_warnings")
        assert "ix_chunks_tsv_english_fts" not in {i["name"] for i in inspect(engine).get_indexes("chunks")}
        command.upgrade(cfg, "head")
        with engine.connect() as connection:
            assert connection.execute(text("SELECT id,text,locator,embedding::text FROM chunks")).all() == before
            assert connection.execute(text("SELECT structure FROM chunks")).scalar_one() == {}
            assert connection.execute(text("SELECT to_regclass('ix_chunks_tsv_english_fts')")).scalar_one()
    finally:
        engine.dispose()
        get_settings.cache_clear()
