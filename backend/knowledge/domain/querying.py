"""保留原问题的受控查询规划；只提取用户已提供的标识和子问题。"""

from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass

_IDENTIFIER = re.compile(
    r"(?<![A-Za-z0-9_-])[A-Za-z][A-Za-z0-9]*(?:[-_/][A-Za-z0-9]+)+(?![A-Za-z0-9_-])"
)
_ARTICLE = re.compile(r"第[一二三四五六七八九十百千万零〇\d]+条")
_FIELD_SEPARATOR = re.compile(r"、(?:以及|及|和)?|[,，]\s*(?:以及|及|和)")
_AMBIGUOUS_SCOPE = re.compile(
    r"排除|除外|除非|不含|不要|不得|不包括|不适用|而非|只(?:查|看|限)|仅|"
    r"\b(?:not|except|unless|excluding|without|only)\b", re.IGNORECASE,
)
_FIELD_LIST = re.compile(r"^(?:核对|请核对|查询|请查询|请列出|列出)\s*(.{2,60}?的)\s*(.+)$")


@dataclass(frozen=True)
class QueryPlan:
    original: str
    variants: tuple[str, ...]
    exact_terms: tuple[str, ...]
    intent: str


def evidence_requirements(query: str) -> tuple[str, ...]:
    """只拆有显式共同对象的字段清单，或可以独立阅读的并列问句。"""
    query = " ".join(unicodedata.normalize("NFKC", query).split())
    if _AMBIGUOUS_SCOPE.search(query):
        return ()
    field_list = _FIELD_LIST.fullmatch(query.rstrip("。.!?"))
    if field_list and "?" not in query:
        scope, fields = field_list.groups()
        parts = tuple(part.strip() for part in _FIELD_SEPARATOR.split(fields))
        if 2 <= len(parts) <= 3 and all(
            2 <= len(part) <= 48 and not re.search(r"[,，;；。]", part) for part in parts
        ):
            return tuple(f"{scope} {part}" for part in parts)
    if not query.endswith("?"):
        return ()
    parts = tuple(part.strip() for part in query[:-1].split("?"))
    # 无显式共同前缀时，只拆独立含问词的问句，编号或条款不能丢失。
    terms = set(_IDENTIFIER.findall(query)) | set(_ARTICLE.findall(query))
    if 2 <= len(parts) <= 3 and all(
        len(part) >= 8
        and re.search(r"哪|谁|何时|何处|多少|什么|几|如何|\b(?:what|where|when|who|how)\b",
                      part, re.IGNORECASE)
        and terms <= (set(_IDENTIFIER.findall(part)) | set(_ARTICLE.findall(part)))
        and not re.search(r"^(?:同时|以及|另外|还有|其|它|该|and\b|also\b|it\b)", part, re.IGNORECASE)
        for part in parts
    ):
        return parts
    return ()


def plan_query(query: str, max_variants: int = 3) -> QueryPlan:
    normalized = " ".join(unicodedata.normalize("NFKC", query).split())
    exact = tuple(dict.fromkeys([*_ARTICLE.findall(normalized), *_IDENTIFIER.findall(normalized)]))[
        :4
    ]
    variants = list(dict.fromkeys([query, normalized]))
    # 只拆显式并列问句；不删除“不、除外、至少”等约束，不猜测省略的主语。
    parts = [part.strip() for part in re.split(r"[；;？?]", normalized) if part.strip()]
    if 1 < len(parts) <= 3 and all(len(part) >= 6 for part in parts):
        variants.extend(parts)
    intent = "exact" if exact else "multi" if len(parts) > 1 else "semantic"
    return QueryPlan(query, tuple(dict.fromkeys(variants))[:max_variants], exact, intent)
