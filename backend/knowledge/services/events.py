"""编排资料变更订阅，失效信号由客户端通过授权 REST 重新同步。"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from knowledge.config import Settings
from knowledge.domain.entities import Actor
from knowledge.repositories import events as event_repo


@asynccontextmanager
async def subscribe(
    actor: Actor, settings: Settings,
) -> AsyncIterator[AsyncIterator[event_repo.ChangeEvent]]:
    async with event_repo.subscribe(actor, settings.database_url) as changes:
        yield changes
