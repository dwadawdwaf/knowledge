from __future__ import annotations

import pathlib
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from knowledge.config import Settings
from knowledge.domain.entities import Actor


@pytest.fixture
def settings(tmp_path) -> Settings:
    return Settings(
        database_url="postgresql+psycopg://unused:unused@localhost:5432/unused",
        storage_root=str(tmp_path / "objects"),
        token_secret="test-secret",
        embedding_dim=1024,
    )


@pytest.fixture
def actor() -> Actor:
    return Actor(subject_id="user-a", tenant_id="default")
