import os
import pathlib
import uuid

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import DBAPIError

from knowledge.config import get_settings

URL = os.getenv("KS_TEST_MIGRATION_DATABASE_URL")
pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not URL, reason="isolated migration DB required"),
]


def test_empty_and_populated_migration(monkeypatch):
    monkeypatch.setenv("KS_DATABASE_URL", URL)
    get_settings.cache_clear()
    cfg = Config()
    cfg.set_main_option(
        "script_location", str(pathlib.Path(__file__).resolve().parents[2] / "migrations")
    )
    engine = create_engine(URL)
    try:
        command.upgrade(cfg, "head")
        assert {"build_id", "generation"} <= {
            c["name"] for c in inspect(engine).get_columns("jobs")
        }
        command.downgrade(cfg, "0001_initial")
        job_id = uuid.uuid4()
        conversation_id = uuid.uuid4()
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO jobs(id,kind,resource_id,state) VALUES (:id,'ingest',:doc,'queued')"
                ),
                {"id": job_id, "doc": uuid.uuid4()},
            )
            connection.execute(
                text("INSERT INTO conversations(id, owner_id, title) VALUES (:id, 'owner', '历史')"),
                {"id": conversation_id},
            )
            connection.execute(
                text(
                    "INSERT INTO messages(id, conversation_id, role, body) "
                    "VALUES (:id, :conversation_id, 'assistant', '原有回答')"
                ),
                {"id": uuid.uuid4(), "conversation_id": conversation_id},
            )
        with pytest.raises(DBAPIError, match="Drain ingest/reindex"):
            command.upgrade(cfg, "head")
        assert "build_id" not in {c["name"] for c in inspect(engine).get_columns("jobs")}
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE jobs SET state='succeeded' WHERE id=:id"), {"id": job_id}
            )
        command.upgrade(cfg, "head")
        with engine.begin() as connection:
            assert (
                connection.execute(
                    text("SELECT state FROM jobs WHERE id=:id"), {"id": job_id}
                ).scalar_one()
                == "succeeded"
            )
            assert connection.execute(
                text("SELECT status FROM conversations WHERE id=:id"), {"id": conversation_id}
            ).scalar_one() == "active"
            connection.execute(
                text("UPDATE conversations SET status='archived' WHERE id=:id"),
                {"id": conversation_id},
            )
            model = "test-model-" + "a" * 100
            row = connection.execute(
                text(
                    "UPDATE messages SET model_revision=:model WHERE conversation_id=:id "
                    "RETURNING body, model_revision"
                ),
                {"id": conversation_id, "model": model},
            ).one()
            assert tuple(row) == ("原有回答", model)
    finally:
        engine.dispose()
        get_settings.cache_clear()
