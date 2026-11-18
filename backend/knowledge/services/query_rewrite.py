"""上下文感知检索：把代词、省略与「继续」类问题补成可独立检索的问题。

第一版刻意保持确定性，不调用模型：
原始问题原样保留在结果里，只在前面补上上一轮的问题作为上文，因此不会改写用户语义，
也不会引入模型幻觉。是否补全只由表面线索决定，失败时回退为原始问题。
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

from knowledge.domain.entities import ContextMessage

# 指代与省略线索：出现任一词才认为该问题依赖上文。
REFERENCE_MARKERS = (
    "它",
    "他们",
    "她们",
    "这个",
    "那个",
    "这些",
    "那些",
    "此",
    "该",
    "上述",
    "上面",
    "前述",
    "前面",
    "刚才",
    "刚刚",
    "之前",
    "继续",
    "接着",
    "展开",
    "再说",
    "还有",
    "同样",
    "另外",
    "区别是什么",
)


@dataclass(frozen=True)
class StandaloneQuery:
    """检索用的问题。`text` 一定包含原始问题；`rewritten` 表示是否补了上文。"""

    text: str
    original: str
    rewritten: bool = False
    context_messages: int = 0
    # 补全时使用的那段上文；召回落空时可只用它再取一次。
    anchor: str = ""


def looks_context_dependent(question: str) -> bool:
    """是否出现指代/省略线索。"""
    return any(marker in question for marker in REFERENCE_MARKERS)


def _anchor(history: Sequence[ContextMessage]) -> str:
    """选上一轮用户问题作为上文；没有时退回上一条助手回答。"""
    for item in reversed(history):
        if item.role == "user" and item.text.strip():
            return item.text.strip()
    for item in reversed(history):
        if item.role == "assistant" and item.text.strip():
            return item.text.strip()
    return ""


def standalone_query(
    history: Sequence[ContextMessage],
    question: str,
    *,
    anchor_chars: int,
    query_chars: int,
) -> StandaloneQuery:
    """在需要时把上文拼到问题前面；只补上文，不改原句。"""
    original = (question or "").strip()
    base = StandaloneQuery(text=original, original=original, context_messages=len(history))
    if not original or anchor_chars <= 0 or not history:
        return base
    if not looks_context_dependent(original):
        return base

    anchor = _anchor(history)
    if not anchor:
        return base
    roof = min(anchor_chars, max(query_chars - len(original) - 1, 0))
    if roof <= 0:
        return base
    context = " ".join(anchor.split())[:roof]
    if not context:
        return base
    return StandaloneQuery(
        text=f"{context} {original}",
        original=original,
        rewritten=True,
        context_messages=len(history),
        anchor=context,
    )
