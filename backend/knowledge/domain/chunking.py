"""分块正文与原文定位的共享表示。"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass(frozen=True)
class TextChunk:
    ordinal: int
    text: str
    locator: dict
    warnings: tuple[str, ...] = ()
    structure: dict[str, Any] = field(default_factory=dict)
