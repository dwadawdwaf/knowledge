"""集成用例共享夹具。

需要真实 PostgreSQL（含 pgvector、pg_trgm）。设置 KS_TEST_DATABASE_URL 后运行：
    KS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/ks_test pytest -m integration
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from knowledge.adapters.embedding import HashEmbedder
from knowledge.adapters.parser import build_parser_registry
from knowledge.adapters.storage import LocalStorage
from knowledge.config import Settings
from knowledge.repositories.tables import Base
from knowledge.worker.loop import WorkerDeps

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")


@pytest.fixture
def session():
    """每个用例重建 schema，避免上一个用例的排队任务被误领。"""
    engine = create_engine(DATABASE_URL, future=True)
    with engine.begin() as conn:
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector"))
        conn.execute(text("CREATE EXTENSION IF NOT EXISTS pg_trgm"))
    Base.metadata.drop_all(engine)
    Base.metadata.create_all(engine)

    with Session(engine, expire_on_commit=False) as active:
        yield active

    Base.metadata.drop_all(engine)
    engine.dispose()


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url=DATABASE_URL or "postgresql+psycopg://unused:unused@localhost:5432/unused",
        storage_root=str(tmp_path / "objects"),
        token_secret="test-secret",
        evidence_min_score=0.0,
    )


@pytest.fixture
def deps(settings) -> WorkerDeps:
    return WorkerDeps(
        settings=settings,
        storage=LocalStorage(settings.storage_root),
        registry=build_parser_registry(),
        embedder=HashEmbedder(dimension=settings.embedding_dim),
    )
