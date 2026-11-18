"""答案正文中的证据标记：`[E1]` 由服务端证据键映射，模型不得生成真实 ID。

这里只做纯文本处理，不依赖框架或数据库，供流式与非流式路径共用。
"""

from __future__ import annotations

import re

MARKER = re.compile(r"\[E(\d+)\]")
NO_EVIDENCE = "NO_EVIDENCE"
# 一个增量末尾可能是半个标记（"[E"、"["），先扣住等下一块再判断，避免把标记碎片推给前端。
_PARTIAL = re.compile(r"\[(?:E\d*)?$")


def marker_keys(text: str) -> tuple[str, ...]:
    """按出现顺序去重返回证据键，例如 "a[E2]b[E1]" → ("E2", "E1")。"""
    seen: dict[str, None] = {}
    for match in MARKER.finditer(text):
        seen.setdefault(f"E{int(match.group(1))}")
    return tuple(seen)


def strip_markers(text: str) -> str:
    """去掉标记，仅保留正文；用于把模型原文还原成可读文本。"""
    return MARKER.sub("", text)


class MarkerStream:
    """增量过滤 `[E#]`：完整标记立即移除，跨增量的半个标记留到下一块。"""

    def __init__(self) -> None:
        self._pending = ""

    def feed(self, delta: str) -> str:
        buffer = self._pending + delta
        self._pending = ""
        buffer = MARKER.sub("", buffer)
        partial = _PARTIAL.search(buffer)
        if partial:
            self._pending = buffer[partial.start():]
            buffer = buffer[: partial.start()]
        return buffer

    def flush(self) -> str:
        """流结束后吐出被扣住的尾部；它不是完整标记，按正文处理。"""
        tail, self._pending = self._pending, ""
        return tail
