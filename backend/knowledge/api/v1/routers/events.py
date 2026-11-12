"""资料失效事件流，Authorization 与现有 REST 使用同一验证器。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from threading import BoundedSemaphore

from fastapi import APIRouter, Depends, Request
from fastapi.responses import StreamingResponse

from knowledge.api.deps import get_actor, get_settings_dep
from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.domain.errors import AuthenticationFailed, EventStreamUnavailable
from knowledge.security import bearer_actor
from knowledge.services import events as event_service

router = APIRouter(tags=["events"])


async def subscribed_changes(
    request: Request,
    actor: Actor = Depends(get_actor), settings: Settings = Depends(get_settings_dep),
) -> AsyncIterator[AsyncIterator[str]]:
    # 每个 API 进程限制长连接，给普通 REST 请求保留线程与数据库连接。
    if not hasattr(request.app.state, "event_slots"):
        request.app.state.event_slots = BoundedSemaphore(settings.event_stream_limit)
    slots = request.app.state.event_slots
    if not slots.acquire(blocking=False):
        raise EventStreamUnavailable("实时连接数已达上限，请关闭多余资料页后重试")
    try:
        async with event_service.subscribe(actor, settings) as changes:
            yield changes
    finally:
        slots.release()


@router.get("/events", response_class=StreamingResponse)
async def events(
    request: Request,
    changes: AsyncIterator[str] = Depends(subscribed_changes),
    settings: Settings = Depends(get_settings_dep),
) -> StreamingResponse:
    async def encode() -> AsyncIterator[str]:
        async for kind in changes:
            try:
                bearer_actor(request.headers.get("Authorization"), settings)
            except AuthenticationFailed:
                yield 'event: expired\ndata: {"version":1}\n\n'
                return
            yield f'event: {kind}\ndata: {{"version":1}}\n\n'

    return StreamingResponse(encode(), media_type="text/event-stream", headers={
        "Cache-Control": "no-store", "X-Accel-Buffering": "no",
    })
