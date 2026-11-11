"""词项覆盖特征。算法参照 RAGFlow v0.24.0 的 term_weight.py 和 query.py。"""

from __future__ import annotations

import math
import re
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass, field
from itertools import pairwise, product

_NUMBERS = re.compile(r"[0-9. -]{2,}")
_ENGLISH = re.compile(r"[a-z. -]+")
_NUMERIC_ENTITY = re.compile(r"[0-9,.]{2,}")
_SHORT_LETTER = re.compile(r"[a-z]{1,2}")
_NUMERIC_TAG = re.compile(r"[0-9-]+")
_ENTITY_WEIGHTS = {"toxic": 2, "corp": 3, "loca": 3, "sch": 3, "stock": 3}
LEXICAL_REVISION = 7
FIELD_WEIGHTS = {"title": 10, "title_fine": 5, "important": 20, "content": 2, "content_fine": 1}

# 召回使用的主词、同义词、归一化词权重、同义词相对权重。
Feature = tuple[tuple[str, ...], tuple[str, ...], float, float]


@dataclass(frozen=True)
class QueryTerms:
    groups: list[list[str]]
    features: dict[str, float]
    recall_features: list[Feature]
    use_minimum_match: bool = True
    proximity: dict[str, tuple[str, ...]] = field(default_factory=dict)
    # 每个外层子句引用其中词组的主词，组内任一词组命中即计一次。
    clauses: tuple[tuple[str, ...], ...] = ()
    whole_phrases: dict[tuple[str, ...], float] = field(default_factory=dict)
    clause_aliases: dict[int, dict[str, float]] = field(default_factory=dict)
    clause_phrases: dict[int, tuple[str, ...]] = field(default_factory=dict)

    @property
    def has_recall(self) -> bool:
        return bool(self.groups or self.clause_phrases)

    def coverage(self, document_terms: Iterable[str]) -> float:
        terms = set(document_terms)
        return sum(weight for term, weight in self.features.items() if term in terms)


def term_weight(
    term: str, *, frequency: Callable[[str], int], fine_grained: Callable[[str], str], tag: str, entity: str = "",
) -> float:
    """移植上游词频回退、IDF、词性及实体加权。"""
    def frequencies(word: str) -> tuple[float, float]:
        if _NUMBERS.fullmatch(word):
            return 3, 5
        corpus = frequency(word)
        if _ENGLISH.fullmatch(word):
            return max(corpus, 10) if corpus else 300, 300

        # 固定版上游没有附带 term.freq，使用其空文档频率表分支。
        document = 3.0
        parts = [part for part in fine_grained(word).split() if 1 < len(part) < len(word)] if len(word) >= 4 else []
        if len(parts) > 1:
            estimates = [frequencies(part) for part in parts]
            corpus_estimate = corpus or min(value for value, _ in estimates) / 6
            document = max(3.0, min(value for _, value in estimates) / 6)
            return max(corpus_estimate, 10), document
        return max(corpus, 10), document

    corpus_frequency, document_frequency = frequencies(term)
    entity_weight = (
        2 if _NUMERIC_ENTITY.fullmatch(term)
        else 0.01 if _SHORT_LETTER.fullmatch(term)
        else _ENTITY_WEIGHTS.get(entity, 1)
    )
    pos_weight = (
        0.3 if tag in {"r", "c", "d"}
        else 3 if tag in {"ns", "nt"}
        else 2 if tag == "n" or _NUMERIC_TAG.match(tag) else 1
    )

    def idf(count: float, population: int) -> float:
        return math.log10(10 + (population - count + 0.5) / (count + 0.5))

    return entity_weight * pos_weight * (
        0.3 * idf(corpus_frequency, 10_000_000) + 0.7 * idf(document_frequency, 1_000_000_000)
    )


def pair_key(left: str, right: str) -> str:
    return f"pair:{left} {right}"


def ranking_keys(words: Sequence[str]) -> set[str]:
    return {*words, *(pair_key(left, right) for left, right in pairwise(words))}


def ranking_weights(words: Sequence[tuple[str, float]]) -> dict[str, float]:
    """移植 token_similarity 的词项和相邻词组覆盖公式。"""
    weights: dict[str, float] = {}
    for word, weight in words:
        weights[word] = weights.get(word, 0.0) + 0.4 * weight
    for (left, left_weight), (right, right_weight) in pairwise(words):
        key = pair_key(left, right)
        weights[key] = weights.get(key, 0.0) + 0.6 * max(left_weight, right_weight)
    total = sum(weights.values())
    return {key: weight / total for key, weight in weights.items()}


def phrase_key(words: tuple[str, ...]) -> str:
    if len(words) == 1:
        return words[0]
    if len(words) == 2:
        return pair_key(*words)
    return "phrase:" + " ".join(words)


def position_key(term: str) -> str:
    return "w" + term.encode("utf-8").hex()


def phrase_queries(words: tuple[str, ...]) -> tuple[str, str]:
    """原版中文分支使用严格短语和 slop=2 的降权短语。"""
    keys = tuple(map(position_key, words))

    def expression(positions: list[tuple[int, str]]) -> str:
        return positions[0][1] + "".join(
            f" <{right - left}> {key}" for (left, _), (right, key) in pairwise(positions)
        )

    exact = expression(list(enumerate(keys)))
    variants = {exact}
    # Lucene 比较各词「实际位置减查询位置」后的最大与最小差值。
    # 分词器只细分不超过 10 字的词，保留的每段至少两字。
    for shifts in product(range(3), repeat=len(keys)):
        if min(shifts) != 0:
            continue
        positions = sorted((index + shift, keys[index]) for index, shift in enumerate(shifts))
        if len({position for position, _ in positions}) == len(keys):
            variants.add(expression(positions))
    return exact, " | ".join(f"({value})" for value in sorted(variants))


def build_query_terms(
    ordered: list[list[str]], weights: list[float], *,
    ranking: Sequence[tuple[str, float]],
    synonyms: dict[str, tuple[str, ...]] | None = None, chinese: bool = False,
    proximity: dict[str, tuple[str, ...]] | None = None,
    clauses: tuple[tuple[str, ...], ...] = (),
    whole_phrases: Sequence[tuple[tuple[str, ...], float]] = (),
    clause_aliases: dict[int, tuple[str, ...]] | None = None,
    clause_phrases: dict[int, tuple[str, ...]] | None = None,
    english_terms: Sequence[tuple[str, float]] | None = None,
) -> QueryTerms:
    """累加单词和相邻词组权重，关键词候选使用独立的短语权重。"""
    lexical: dict[tuple[str, ...], float] = {}
    groups = {}
    synonyms = synonyms or {}
    for group, weight in zip(ordered, weights, strict=True):
        primary = group[:1] if proximity and group[0] in proximity else group
        groups[group[0]] = list(dict.fromkeys([*primary, *synonyms.get(group[0], ())]))
        recall_variants = tuple(primary)
        lexical[recall_variants] = lexical.get(recall_variants, 0.0) + weight
    if not chinese:
        sequence = english_terms if english_terms is not None else list(
            zip((group[0] for group in ordered), weights, strict=True)
        )
        single_terms = set(groups)
        for (left, left_weight), (right, right_weight) in pairwise(sequence):
            variants = (pair_key(left, right),)
            lexical[variants] = lexical.get(variants, 0.0) + 2.0 * max(left_weight, right_weight)
            if left not in single_terms and right not in single_terms:
                groups[variants[0]] = list(variants)
    phrase_weights: dict[tuple[str, ...], float] = {}
    for phrase, weight in whole_phrases:
        phrase_weights[phrase] = phrase_weights.get(phrase, 0.0) + weight
    clause_aliases = clause_aliases or {}
    total = sum(lexical.values()) + sum(phrase_weights.values()) + 0.7 * sum(map(len, clause_aliases.values()))
    recall_features = [
        (variants, synonyms.get(variants[0], ()), weight / total, 0.2 if chinese else 0.25)
        for variants, weight in lexical.items()
    ]

    return QueryTerms(
        list(groups.values()), ranking_weights(ranking), recall_features, chinese, proximity or {}, clauses,
        {phrase: weight / total for phrase, weight in phrase_weights.items()},
        {clause: dict.fromkeys(aliases, 0.7 / total) for clause, aliases in clause_aliases.items()},
        clause_phrases or {},
    )
