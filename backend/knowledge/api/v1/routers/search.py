"""检索接口：不调用生成模型。"""

from __future__ import annotations

from fastapi import APIRouter, Depends, Request
from sqlalchemy.orm import Session

from knowledge.api.deps import (
    get_actor,
    get_embedder,
    get_keyword_index,
    get_reranker,
    get_session,
    get_settings_dep,
    request_id,
)
from knowledge.api.v1.schemas.common import Envelope, envelope
from knowledge.api.v1.schemas.requests import SearchRequest
from knowledge.api.v1.schemas.responses import SearchHitOut, SearchResultOut
from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.domain.ports import Embedder, KeywordIndex, Reranker
from knowledge.services import retrieval

router = APIRouter(tags=["search"])


@router.post("/search", response_model=Envelope)
def search(
    payload: SearchRequest,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
    embedder: Embedder = Depends(get_embedder),
    reranker: Reranker | None = Depends(get_reranker),
    keyword_index: KeywordIndex | None = Depends(get_keyword_index),
) -> dict:
    hits = retrieval.search(
        session,
        actor,
        settings,
        embedder,
        query=payload.query,
        kb_ids=payload.knowledge_base_ids,
        mode=payload.mode,
        top_k=payload.top_k,
        reranker=reranker,
        keyword_index=keyword_index,
    )
    result = SearchResultOut(
        mode=payload.mode,
        hits=[
            SearchHitOut(
                document_id=hit.document_id,
                kb_id=hit.kb_id,
                kb_name=hit.kb_name,
                version_id=hit.version_id,
                build_id=hit.build_id,
                chunk_id=hit.chunk_id,
                title=hit.title,
                excerpt=hit.excerpt,
                locator=hit.locator,
                score=round(hit.score, 6),
                score_type=hit.score_type,
            )
            for hit in hits
        ],
        score_type=hits[0].score_type if hits else payload.mode,
        warnings=sorted({warning for hit in hits for warning in hit.warnings}),
        degraded=any(hit.warnings for hit in hits),
    )
    return envelope(result.model_dump(mode="json"), request_id(request))
