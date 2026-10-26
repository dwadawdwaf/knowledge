"""文档接口：上传、列表、详情、新版本、同模型重建、删除。"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, File, Request, Response, UploadFile
from sqlalchemy.orm import Session

from knowledge.api.deps import (
    get_actor,
    get_embedder,
    get_registry,
    get_session,
    get_settings_dep,
    get_storage,
    request_id,
)
from knowledge.api.v1.schemas.common import Envelope, envelope
from knowledge.api.v1.schemas.requests import ReindexRequest
from knowledge.api.v1.schemas.responses import ReindexAck, UploadAck
from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.domain.errors import FileTooLarge
from knowledge.domain.ports import Embedder, ObjectStorage
from knowledge.logging import get_logger
from knowledge.services import document as document_service

router = APIRouter(tags=["documents"])
log = get_logger("api.documents")


@router.post("/documents/{document_id}/reindex", status_code=202, response_model=Envelope)
def reindex(
    document_id: uuid.UUID,
    body: ReindexRequest,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
    embedder: Embedder = Depends(get_embedder),
) -> dict:
    result = document_service.reindex(
        session,
        actor,
        settings,
        document_id=document_id,
        version_id=body.version_id,
        expected_generation=body.expected_generation,
        embedder_model=embedder.model,
        embedder_dim=embedder.dimension,
    )
    log.info(
        "reindex_enqueued",
        extra={
            "request_id": request_id(request),
            "job_id": str(result.job_id),
            "build_id": str(result.build_id),
            "generation": result.generation,
        },
    )
    return envelope(
        ReindexAck(
            document_id=result.document_id,
            version_id=result.version_id,
            build_id=result.build_id,
            job_id=result.job_id,
            generation=result.generation,
        ).model_dump(mode="json"),
        request_id(request),
    )


async def _read(file: UploadFile, limit: int) -> bytes:
    data = await file.read(limit + 1)
    if len(data) > limit:
        raise FileTooLarge("文件超过大小上限")
    return data


@router.post("/knowledge-bases/{kb_id}/documents", status_code=202, response_model=Envelope)
async def upload(
    kb_id: uuid.UUID,
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
    storage: ObjectStorage = Depends(get_storage),
    registry=Depends(get_registry),
    embedder: Embedder = Depends(get_embedder),
) -> dict:
    data = await _read(file, settings.max_upload_bytes)
    result = document_service.upload(
        session,
        actor,
        settings,
        storage,
        registry,
        kb_id=kb_id,
        filename=file.filename or "upload",
        data=data,
        embedder_model=embedder.model,
        embedder_dim=embedder.dimension,
    )
    if result.deduplicated:
        response.status_code = 200
    return envelope(
        UploadAck(
            document_id=result.document_id,
            version_id=result.version_id,
            build_id=result.build_id,
            job_id=result.job_id,
            deduplicated=result.deduplicated,
        ).model_dump(mode="json"),
        request_id(request),
    )


@router.get("/knowledge-bases/{kb_id}/documents", response_model=Envelope)
def list_documents(
    kb_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    items = document_service.list_in_kb(session, actor, kb_id)
    return envelope({"items": items}, request_id(request))


@router.get("/documents/{document_id}", response_model=Envelope)
def detail(
    document_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
) -> dict:
    return envelope(document_service.detail(session, actor, document_id), request_id(request))


@router.post("/documents/{document_id}/versions", status_code=202, response_model=Envelope)
async def add_version(
    document_id: uuid.UUID,
    request: Request,
    response: Response,
    file: UploadFile = File(...),
    expected_generation: int | None = None,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
    storage: ObjectStorage = Depends(get_storage),
    registry=Depends(get_registry),
    embedder: Embedder = Depends(get_embedder),
) -> dict:
    data = await _read(file, settings.max_upload_bytes)
    result = document_service.add_version(
        session,
        actor,
        settings,
        storage,
        registry,
        document_id=document_id,
        filename=file.filename or "upload",
        data=data,
        expected_generation=expected_generation,
        embedder_model=embedder.model,
        embedder_dim=embedder.dimension,
    )
    if result.deduplicated:
        response.status_code = 200
    return envelope(
        UploadAck(
            document_id=result.document_id,
            version_id=result.version_id,
            build_id=result.build_id,
            job_id=result.job_id,
            deduplicated=result.deduplicated,
        ).model_dump(mode="json"),
        request_id(request),
    )


@router.delete("/documents/{document_id}", status_code=202, response_model=Envelope)
def delete(
    document_id: uuid.UUID,
    request: Request,
    session: Session = Depends(get_session),
    actor: Actor = Depends(get_actor),
    settings: Settings = Depends(get_settings_dep),
) -> dict:
    job_id, status = document_service.delete(session, actor, document_id, settings.max_attempts)
    return envelope(
        {"id": str(document_id), "status": status, "cleanup_job_id": str(job_id)},
        request_id(request),
    )
