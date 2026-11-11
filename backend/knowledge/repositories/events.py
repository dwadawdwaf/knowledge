"""使用独立连接订阅 PostgreSQL 提交通知，不持有业务事务。"""

from __future__ import annotations

import hashlib
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from functools import partial
from typing import Literal

import anyio
import psycopg
from psycopg import sql

from knowledge.domain.entities import Actor
from knowledge.domain.errors import EventStreamUnavailable

ChangeEvent = Literal["sync", "change", "heartbeat"]


@asynccontextmanager
async def subscribe(actor: Actor, database_url: str) -> AsyncIterator[AsyncIterator[ChangeEvent]]:
    """通道只从已验证的 Actor 推导，调用者不能指定他人通道。"""
    channel = "ks_changes_" + hashlib.md5(actor.subject_id.encode(), usedforsecurity=False).hexdigest()
    dsn = database_url.replace("postgresql+psycopg://", "postgresql://", 1)
    connection = None
    try:
        # 复用现有线程池，兼容 Windows Proactor 事件循环。
        # run_sync 等待完成后才响应取消，连接创建与释放不会被遗弃在线程中。
        connection = await anyio.to_thread.run_sync(partial(
            psycopg.connect, dsn, autocommit=True, connect_timeout=5,
            application_name="ks-document-events",
        ))

        def prepare() -> None:
            cursor = connection.execute(
                "SELECT to_regprocedure('ks_notify_document_change()') IS NOT NULL"
            )
            installed = cursor.fetchone()
            if not installed or not installed[0]:
                raise EventStreamUnavailable("资料实时通知尚未迁移，请先升级数据库")
            connection.execute(sql.SQL("LISTEN {}").format(sql.Identifier(channel)))

        await anyio.to_thread.run_sync(prepare)

        def wait_for_change() -> bool:
            # 只等待 socket 通知，不执行查询。最长一秒交还控制以回收断开的连接。
            return bool(list(connection.notifies(timeout=1, stop_after=1)))

        async def changes() -> AsyncIterator[ChangeEvent]:
            yield "sync"
            last_sent = time.monotonic()
            while True:
                changed = await anyio.to_thread.run_sync(wait_for_change)
                if changed:
                    last_sent = time.monotonic()
                    yield "change"
                elif time.monotonic() - last_sent >= 15:
                    last_sent = time.monotonic()
                    yield "heartbeat"

        yield changes()
    except psycopg.Error:
        raise EventStreamUnavailable("实时状态连接暂不可用，请稍后重试或手动刷新") from None
    finally:
        if connection is not None:
            connection.close()
