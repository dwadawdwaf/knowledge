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
    finally:
        engine.dispose()
        get_settings.cache_clear()
