"""集成用例共享夹具。

需要真实 PostgreSQL（含 pgvector、pg_trgm）。设置 KS_TEST_DATABASE_URL 后运行：
    KS_TEST_DATABASE_URL=postgresql+psycopg://user:pass@localhost:5432/ks_test pytest -m integration
默认用本地哈希向量保持用例封闭；设 KS_TEST_REAL_EMBEDDER=1 时改用 .env 配置的真实向量端点
（需配合 NO_PROXY=127.0.0.1,localhost 访问本机 Ollama）。
"""

from __future__ import annotations

import os

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import Session

from knowledge.adapters.embedding import HashEmbedder, build_embedder
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
    overrides: dict[str, object] = {
        "database_url": DATABASE_URL
        or "postgresql+psycopg://unused:unused@localhost:5432/unused",
        "storage_root": str(tmp_path / "objects"),
        "token_secret": "test-secret",
        "evidence_min_score": 0.0,
    }
    if not os.getenv("KS_TEST_REAL_EMBEDDER"):
        # 默认封闭运行用哈希向量，阈值钉在哈希标定值（config 默认），
        # 避免 .env 里为真实向量模型标定的阈值把哈希命中全部滤掉
        overrides.update(similarity_floor=0.06, semantic_max_distance=0.85)
    return Settings(**overrides)


@pytest.fixture
def deps(settings) -> WorkerDeps:
    if os.getenv("KS_TEST_REAL_EMBEDDER"):
        embedder = build_embedder(settings)
    else:
        embedder = HashEmbedder(dimension=settings.embedding_dim)
    return WorkerDeps(
        settings=settings,
        storage=LocalStorage(settings.storage_root),
        registry=build_parser_registry(),
        embedder=embedder,
    )
