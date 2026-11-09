"""依赖装配：请求级 Session、Actor、适配器单例。"""

from __future__ import annotations

from collections.abc import Iterator
from functools import lru_cache

from fastapi import Depends, Header, Request
from sqlalchemy.orm import Session

from knowledge.adapters.embedding import build_embedder
from knowledge.adapters.generator import build_generator
from knowledge.adapters.opensearch import build_keyword_index
from knowledge.adapters.parser import build_parser_registry
from knowledge.adapters.reranking import build_reranker
from knowledge.adapters.storage import LocalStorage
from knowledge.config import Settings, get_settings
from knowledge.domain.entities import Actor
from knowledge.domain.ports import Embedder, Generator, KeywordIndex, ObjectStorage, Reranker
from knowledge.repositories.session import new_session
from knowledge.security import bearer_actor


def get_settings_dep() -> Settings:
    return get_settings()


def get_session() -> Iterator[Session]:
    session = new_session()
    try:
        yield session
    finally:
        session.close()


def get_actor(
    authorization: str | None = Header(default=None),
    settings: Settings = Depends(get_settings_dep),
) -> Actor:
    return bearer_actor(authorization, settings)


def request_id(request: Request) -> str:
    return getattr(request.state, "request_id", "")


@lru_cache
def get_storage() -> ObjectStorage:
    return LocalStorage(get_settings().storage_root)


@lru_cache
def get_registry():
    return build_parser_registry()


@lru_cache
def get_embedder() -> Embedder:
    return build_embedder(get_settings())


@lru_cache
def get_generator() -> Generator:
    return build_generator(get_settings())


@lru_cache
def get_reranker() -> Reranker | None:
    return build_reranker(get_settings())


def get_keyword_index(settings: Settings = Depends(get_settings_dep)) -> KeywordIndex | None:
    return build_keyword_index(settings)
