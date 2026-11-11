"""按授权用户回填当前发布构建；不重分块或重新向量化。"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from sqlalchemy import create_engine, select
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from knowledge.domain.entities import Actor, Lifecycle
from knowledge.domain.errors import DomainError
from knowledge.repositories.native_search import scope
from knowledge.repositories.tables import KnowledgeBase
from knowledge.services.native_search import synchronize
from scripts.deps import ensure_database
from scripts.real_model import check_ragflow_resources, load_real_settings


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--owner", required=True)
    args = parser.parse_args()
    check_ragflow_resources()
    settings = load_real_settings()
    engine = create_engine(ensure_database(settings, progress=lambda _: None), hide_parameters=True)
    try:
        with Session(engine) as db:
            actor = Actor(args.owner, "maintenance")
            kb_ids = list(db.scalars(select(KnowledgeBase.id).where(
                KnowledgeBase.owner_id == actor.subject_id, KnowledgeBase.status == Lifecycle.ACTIVE,
            )))
            builds = scope(db, actor, kb_ids) if kb_ids else []
            print(f"builds={len(builds)} synchronized={synchronize(db, actor, settings, builds)}")
    finally:
        engine.dispose()


if __name__ == "__main__":
    try:
        main()
    except DomainError as exc:
        print(f"同步失败：{exc.code}；{exc.message}")
        raise SystemExit(1) from None
    except SQLAlchemyError:
        print("同步失败：请检查目标数据库连接及迁移状态")
        raise SystemExit(1) from None
