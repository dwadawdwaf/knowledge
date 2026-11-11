"""仅回填派生词项，不更改历史分块、向量、定位或发布指针。"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path

from sqlalchemy import create_engine, func, select, text
from sqlalchemy.dialects.postgresql import insert
from sqlalchemy.orm import Session

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from knowledge.adapters.ragflow_text import search_fields
from knowledge.domain.entities import BuildState, JobKind, JobState, Lifecycle
from knowledge.domain.ragflow_chunking import STRATEGY
from knowledge.domain.ragflow_query import LEXICAL_REVISION
from knowledge.repositories.ragflow_search import outdated_terms_sql
from knowledge.repositories.tables import (
    Chunk,
    ChunkSearchTerms,
    Document,
    IndexBuild,
    Job,
    KnowledgeBase,
)

_OUTDATED_TERMS = text(outdated_terms_sql(ChunkSearchTerms.__tablename__))


def missing_terms():
    return (
        select(Chunk.id, Chunk.text, Chunk.structure, Document.title)
        .join(IndexBuild, IndexBuild.id == Chunk.build_id)
        .join(Document, Document.active_build_id == IndexBuild.id)
        .join(KnowledgeBase, KnowledgeBase.id == Document.kb_id)
        .outerjoin(ChunkSearchTerms, ChunkSearchTerms.chunk_id == Chunk.id)
        .where(
            IndexBuild.state == BuildState.READY, Document.status == Lifecycle.ACTIVE,
            KnowledgeBase.status == Lifecycle.ACTIVE, _OUTDATED_TERMS,
        )
    )


def count_missing_terms(session: Session) -> int:
    return session.scalar(select(func.count()).select_from(missing_terms().subquery())) or 0


def count_legacy_jobs(session: Session) -> int:
    return session.scalar(
        select(func.count()).select_from(Job).join(IndexBuild, IndexBuild.id == Job.build_id).where(
            Job.kind.in_([JobKind.INGEST, JobKind.REINDEX]),
            Job.state.in_([JobState.QUEUED, JobState.RUNNING]),
            IndexBuild.chunk_config["strategy"].astext.is_distinct_from(STRATEGY),
        )
    ) or 0


def backfill(session: Session, batch_size: int = 250) -> int:
    total = 0
    while rows := session.execute(
        missing_terms().order_by(Chunk.id).limit(batch_size).with_for_update(of=Chunk, key_share=True, read=True)
    ).all():
        entries = []
        for chunk_id, body, structure, title in rows:
            indexed = search_fields(
                body, title, structure.get("section_path") or [], structure.get("table_header") or "",
                row_fields=structure.get("table_fields") or (),
            )
            entries.append({
                "chunk_id": chunk_id, "terms": indexed.terms, "boosted": indexed.boosted,
                "fields": indexed.fields, "lexical_revision": LEXICAL_REVISION,
            })
        statement = insert(ChunkSearchTerms)
        session.execute(statement.on_conflict_do_update(
            index_elements=[ChunkSearchTerms.chunk_id],
            set_={
                "terms": statement.excluded.terms, "boosted": statement.excluded.boosted,
                "fields": statement.excluded.fields, "lexical_revision": statement.excluded.lexical_revision,
            },
            where=_OUTDATED_TERMS,
        ), entries)
        total += len(entries)
        session.commit()
    return total


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="写入回填索引；默认仅检查覆盖")
    args = parser.parse_args()
    url = os.environ.get("KS_DATABASE_URL")
    if not url:
        parser.error("须显式设置 KS_DATABASE_URL")
    engine = create_engine(url, hide_parameters=True)
    try:
        with Session(engine) as session:
            if args.apply:
                print(f"已处理词项回填：{backfill(session)}")
            missing = count_missing_terms(session)
            print(f"当前发布缺少词项索引的分块：{missing}")
            return int(bool(missing))
    finally:
        engine.dispose()


if __name__ == "__main__":
    raise SystemExit(main())

