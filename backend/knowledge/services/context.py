"""会话上下文：只实现 Recent Window（最近 N 条历史），不做摘要与 token 预算。

窗口裁剪与授权读取分离：`trim_window` 是不依赖数据库的纯函数，便于单测；
`load_window` 通过 repository 读取，owner 校验在 repository 内完成。
"""

from __future__ import annotations

import uuid

from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.entities import Actor, ContextMessage
from knowledge.repositories import conversations as conversation_repo

ROLES = frozenset({"user", "assistant"})


def _normalized(text: str) -> str:
    return " ".join((text or "").split())


def trim_window(
    messages: list[ContextMessage], question: str, limit: int
) -> list[ContextMessage]:
    """取最近 limit 条有效历史，并剔除与当前问题重复的尾部 user 消息。

    当前问题通常在生成前已由调用方写入存档；若它也出现在窗口尾部，重复送入会让
    模型看到两次相同提问。只处理尾部，不改动历史中同文的真实提问。
    返回顺序保持原始时间顺序（旧 → 新）。
    """
    if limit <= 0:
        return []
    kept = [item for item in messages if item.role in ROLES and item.text.strip()]
    current = _normalized(question)
    while kept and kept[-1].role == "user" and _normalized(kept[-1].text) == current:
        kept.pop()
    return kept[-limit:]


def load_window(
    session: Session,
    actor: Actor,
    settings: Settings,
    conversation_id: uuid.UUID | None,
    question: str,
) -> list[ContextMessage]:
    """读取当前会话的 Recent Window；无会话或配置为 0 时返回空列表（单轮问答）。"""
    limit = settings.context_recent_messages
    if conversation_id is None or limit <= 0:
        return []
    # 多取一条：当前问题若已存档，最多只会占掉这一条冗余。
    rows = conversation_repo.recent_messages(session, actor, conversation_id, limit + 1)
    return trim_window(
        [ContextMessage(role=row.role, text=row.body or "") for row in rows],
        question,
        limit,
    )
