"""入库与查询共享的本地词项和 token 计数。

查询清洗、子句与词项规则改编自 RAGFlow v0.24.0。
Copyright 2024–2025 The InfiniFlow Authors. Licensed under Apache-2.0.
"""

from __future__ import annotations

import json
import os
import re
import tempfile
import unicodedata
from collections import defaultdict
from collections.abc import Sequence
from dataclasses import dataclass
from functools import lru_cache
from hashlib import sha1, sha256
from itertools import chain, pairwise
from pathlib import Path

import tiktoken
from hanziconv import HanziConv

from knowledge.adapters.ragflow_tokenizer import tokenizer
from knowledge.adapters.ragflow_wordnet import multiword_lemmas, synonyms_for
from knowledge.domain.ragflow_query import (
    QueryTerms,
    build_query_terms,
    pair_key,
    phrase_key,
    position_key,
    ranking_keys,
    term_weight,
)

# ASCII 标识符保持完整。汉字独立分词，避免中英混排时合为一个词。
_RUN = re.compile(r"[\u3400-\u9fff]+|[a-z0-9]+(?:[-_./][a-z0-9]+)*|[^\W_]", re.IGNORECASE)
_HAN = re.compile(r"[\u3400-\u9fff]+")
_STOP = frozenset({
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "how", "in", "is",
    "of", "on", "or", "the", "to", "was", "what", "when", "which", "who", "with",
    "的", "了", "吗", "是", "请问",
})
# RAGFlow v0.24.0 common/query_base.py 和 rag/nlp/term_weight.py 的查询清洗与停用词。
_QUERY_SEPARATORS = re.compile(r"[ :|\r\n\t,，。？?`!！&^%()\[\]{}<>]+|(?<![a-z0-9])/|/(?![a-z0-9])")
_QUERY_FILLERS = (
    (re.compile(
        r"是*(怎么办|什么样的|哪家|一下|那家|请问|啥样|咋样了|什么时候|何时|何地|何人|是否|是不是|"
        r"多少|哪里|怎么|哪儿|怎么样|如何|哪些|是啥|啥是|啊|吗|呢|吧|咋|什么|有没有|呀|谁|哪位|哪个)是*"
    ), ""),
    (re.compile(r"(^| )(what|who|how|which|where|why)('re|'s)? "), " "),
    (re.compile(
        r"(^| )('s|'re|is|are|were|was|do|does|did|don't|doesn't|didn't|has|have|be|there|you|me|your|my|"
        r"mine|just|please|may|i|should|would|wouldn't|will|won't|done|go|for|with|so|the|a|an|by|i'm|it's|"
        r"he's|she's|they|they're|you're|as|on|in|at|up|out|down|of|to|or|and|if) "
    ), " "),
)
_CLAUSE_STOP = frozenset({
    "请问", "您", "你", "我", "他", "是", "的", "就", "有", "于", "及", "即", "在", "为", "最",
    "从", "以", "了", "将", "与", "吗", "吧", "中", "#", "什么", "怎么", "哪个", "哪些", "啥", "相关",
})
_HAN_BOUNDARY = re.compile(r"(?<=[a-z0-9])(?=[\u3400-\u9fff])|(?<=[\u3400-\u9fff])(?=[a-z])")
_ENDS_IN_LETTER = re.compile(r"[a-z]$")
_SHORT_TERM = re.compile(r"[0-9a-z]{1,2}")
_FINE_EXCLUDED = re.compile(r"[0-9a-z.\+#_*/-]+")


@lru_cache(maxsize=1)
def _encoding() -> tiktoken.Encoding:
    cache = os.environ.get("TIKTOKEN_CACHE_DIR", os.environ.get(
        "DATA_GYM_CACHE_DIR", str(Path(tempfile.gettempdir()) / "data-gym-cache"),
    ))
    url = "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken"
    key = sha1(url.encode(), usedforsecurity=False).hexdigest()
    vocabulary = Path(cache) / key
    expected = "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7"
    if not cache or not vocabulary.is_file() or sha256(vocabulary.read_bytes()).hexdigest() != expected:
        raise RuntimeError("cl100k_base 词表缺失或校验失败，请先准备词表缓存")
    return tiktoken.get_encoding("cl100k_base")


def count_tokens(text: str) -> int:
    return len(_encoding().encode_ordinary(text))


@lru_cache(maxsize=8192)
def _english_term(term: str) -> str:
    return tokenizer().english_normalize_([term])[0]


def _han_groups(value: str) -> tuple[tuple[str, ...], ...]:
    analyzer = tokenizer()
    groups = []
    for term in analyzer.tokenize(value).split():
        fine = analyzer.fine_grained_tokenize(term).split()
        groups.append((term,) if fine == [term] else (term, *fine))
    return tuple(groups)


_cached_han_groups = lru_cache(maxsize=16384)(_han_groups)


def _groups(text: str, *, keep_stop: bool = False) -> list[list[str]]:
    groups: list[list[str]] = []
    for match in _RUN.finditer(unicodedata.normalize("NFKC", text).casefold()):
        value = match.group()
        if not keep_stop and value in _STOP:
            continue
        if _HAN.fullmatch(value):
            grouped = _cached_han_groups(value) if len(value) <= 32 else _han_groups(value)
            groups.extend(
                list(group) if keep_stop else [term for term in group if term not in _STOP]
                for group in grouped if keep_stop or group[0] not in _STOP
            )
        elif re.search(r"[-_./]", value):
            groups.append([value])
        else:
            # 保存英文表层词，以便词干不在 WordNet 词头时仍能查询原词。
            normalized = _english_term(value)
            groups.append([normalized] if normalized == value else [normalized, value])
    return groups


def _pairs(groups: list[list[str]]) -> list[str]:
    return [pair_key(left[0], right[0]) for left, right in pairwise(groups)]


_DATA = Path(__file__).with_name("ragflow_data")


def _primary(text: str) -> tuple[str, ...]:
    return tuple(group[0] for group in _groups(text))


def _synonym_words(text: str) -> tuple[str, ...]:
    words = tuple(group[0] for group in _groups(text, keep_stop=True))
    return words if any(word not in _STOP for word in words) else ()


@lru_cache(maxsize=1)
def _entities() -> dict[str, str]:
    source: dict[str, str] = json.loads((_DATA / "ner.json").read_text(encoding="utf-8"))
    return {**{phrase_key(words): entity for name, entity in source.items() if (words := _primary(name))}, **source}


@lru_cache(maxsize=1)
def _synonym_catalog() -> tuple[dict[str, tuple[str, ...]], dict[str, tuple[int, ...]], set[tuple[str, ...]]]:
    source: dict[str, str | list[str]] = json.loads((_DATA / "synonym.json").read_text(encoding="utf-8"))
    synonyms: dict[str, tuple[str, ...]] = {}
    spans: set[tuple[str, ...]] = set()
    for name, variants in source.items():
        words = _primary(name)
        if not words:
            continue
        options = [_synonym_words(alias) for alias in ([variants] if isinstance(variants, str) else variants)[:8]]
        synonyms[phrase_key(words)] = tuple(dict.fromkeys(
            phrase_key(option) for option in options if option and option != words
        ))
        spans.add(words)
        spans.update(option for option in options if option)
    lengths: dict[str, set[int]] = defaultdict(set)
    for span in spans:
        if len(span) > 1:
            lengths[span[0]].add(len(span))
    return synonyms, {word: tuple(sorted(sizes, reverse=True)) for word, sizes in lengths.items()}, spans


def _catalog_matches(words: tuple[str, ...], start: int) -> list[tuple[str, ...]]:
    _, lengths, spans = _synonym_catalog()
    matches = [words[start:start + size] for size in lengths.get(words[start], ()) if start + size <= len(words)]
    return [candidate for candidate in matches if candidate in spans]


def _collapse_catalog(groups: list[list[str]]) -> list[list[str]]:
    words = tuple(group[0] for group in groups)
    ordered = []
    start = 0
    while start < len(words):
        matches = _catalog_matches(words, start)
        if matches:
            phrase = matches[0]
            ordered.append([phrase_key(phrase)])
            start += len(phrase)
        else:
            ordered.append(groups[start])
            start += 1
    return ordered


@lru_cache(maxsize=4096)
def _weight(term: str) -> float:
    analyzer = tokenizer()
    token = _phrase_text(term)
    return term_weight(
        token, frequency=analyzer.freq, fine_grained=analyzer.fine_grained_tokenize,
        tag=analyzer.tag(token), entity=_entities().get(term, _entities().get(token, "")),
    )


def _clean_query(text: str) -> str:
    normalized = HanziConv.toSimplified(unicodedata.normalize("NFKC", text).casefold())
    separated = _QUERY_SEPARATORS.sub(" ", _HAN_BOUNDARY.sub(" ", normalized)).strip()
    cleaned = separated
    for pattern, replacement in _QUERY_FILLERS:
        cleaned = pattern.sub(replacement, cleaned)
    return cleaned.strip() or separated


def _aliases(group: Sequence[str]) -> tuple[str, ...]:
    synonyms, _, _ = _synonym_catalog()
    term = group[0]
    if term in synonyms:
        return synonyms[term]
    aliases = next((found for variant in group if (found := synonyms_for(variant))), ())
    return tuple(dict.fromkeys(phrase_key(words) for alias in aliases if (words := _synonym_words(alias))))


def _clause_words(parts: list[str]) -> list[str]:
    clauses: list[str] = []
    for part in parts:
        if (clauses and _ENDS_IN_LETTER.search(clauses[-1]) and _ENDS_IN_LETTER.search(part)
                and _entities().get(part) != "func" and _entities().get(clauses[-1]) != "func"):
            clauses[-1] += " " + part
        else:
            clauses.append(part)
    return clauses


def _weighted_clause_groups(raw: list[list[str]]) -> list[list[str]]:
    def is_short(word: str) -> bool:
        return len(word) == 1 or _SHORT_TERM.fullmatch(word) is not None

    groups = [group for group in raw if group[0] not in _STOP and group[0] not in _CLAUSE_STOP]
    merged: list[list[str]] = []
    start = 0
    while start < len(groups):
        word = groups[start][0]
        if (start == 0 and is_short(word) and len(groups) > 1 and len(groups[1][0]) > 1
                and not re.match(r"[a-z0-9]", groups[1][0])):
            merged.append([word + " " + groups[1][0]])
            start = 2
            continue
        end = start
        while end < len(groups) and is_short(groups[end][0]):
            end += 1
        count = end - start
        if count > 1:
            stop = end if count < 5 else start + 2
            merged.append([" ".join(group[0] for group in groups[start:stop])])
            start = stop
        else:
            merged.append(groups[start])
            start += 1
    return sorted(merged, key=lambda group: -_weight(group[0]))


def _phrase_text(key: str) -> str:
    return key.split(":", 1)[-1]


def _tokenized_clauses(parts: list[str]) -> list[tuple[str, list[list[str]]]]:
    return [(clause, _groups(clause, keep_stop=True)) for clause in _clause_words(parts)[:256]]


def _clause_aliases(clause: str, groups: list[list[str]]) -> tuple[str, ...]:
    words = tuple(group[0] for group in groups if group[0] not in _STOP)
    if not words:
        return ()
    catalog, _, _ = _synonym_catalog()
    key = phrase_key(words)
    return catalog[key] if key in catalog else _aliases([clause])


@dataclass(frozen=True)
class _ChineseClause:
    terms: list[tuple[list[str], float]]
    aliases: tuple[str, ...]
    phrase: tuple[str, ...]


def _compile_chinese_clauses(
    clauses: list[tuple[str, list[list[str]]]],
) -> tuple[list[str], list[_ChineseClause]]:
    """共用上游关键词计数，保留召回子句与排序词序的不同边界。"""
    keywords: list[str] = []
    compiled: list[_ChineseClause] = []
    for clause, groups in clauses:
        keywords.append(clause)
        aliases = _clause_aliases(clause, groups)
        if len(keywords) < 32:
            keywords.extend(map(_phrase_text, aliases))
        weighted = [(group, _weight(group[0])) for group in _weighted_clause_groups(groups)]
        total = sum(weight for _, weight in weighted)
        selected: list[tuple[list[str], float]] = []
        for group, weight in weighted:
            term = group[0]
            if len(keywords) >= 32:
                break
            keywords.append(re.sub(r"[ \\\"']+", "", term))
            if len(term) >= 3 and not _FINE_EXCLUDED.fullmatch(term):
                fine = tokenizer().fine_grained_tokenize(term).split()
                keywords.extend(word for word in fine if len(word) > 1)
            if len(keywords) < 32:
                keywords.extend(map(_phrase_text, _aliases(group)))
            if len(keywords) >= 32:
                break
            variants = [phrase_key(tuple(term.split()))] if " " in term else group
            selected.append((variants, weight / total))
        words = tuple(group[0] for group in groups)
        phrase = words if (len(weighted) > 1 and len(words) <= 128
                           and all(len(position_key(word)) < 2047 for word in words)) else ()
        if selected or phrase:
            compiled.append(_ChineseClause(selected, aliases, phrase))
    return keywords, compiled


def query_terms(text: str) -> QueryTerms:
    cleaned = _clean_query(text)
    parts = cleaned.split()
    chinese = len(parts) <= 3 or sum(not re.fullmatch(r"[a-z]+", part) for part in parts) * 10 >= len(parts) * 7
    clauses: list[tuple[str, ...]] = []
    whole_phrases: list[tuple[tuple[str, ...], float]] = []
    clause_aliases: dict[int, tuple[str, ...]] = {}
    clause_phrases: dict[int, tuple[str, ...]] = {}
    english_terms = None
    if chinese:
        tokenized = _tokenized_clauses(parts)
        keywords, compiled = _compile_chinese_clauses(tokenized)
        ordered: list[list[str]] = []
        weights: list[float] = []
        for index, clause in enumerate(compiled):
            boost = 5.0 if clause.aliases else 1.0
            if clause.aliases:
                clause_aliases[index] = clause.aliases
            clauses.append(tuple(dict.fromkeys(group[0] for group, _ in clause.terms)))
            ordered.extend(group for group, _ in clause.terms)
            weights.extend(boost * weight for _, weight in clause.terms)
            if clause.phrase:
                whole_phrases.append((clause.phrase, boost * 1.5))
                clause_phrases[index] = clause.phrase
        if not compiled:
            # 上游所有子句均为空时退回原始文本表达式。
            ordered = [group for group in _groups(cleaned) if group[0] not in _CLAUSE_STOP][:128]
            weights = [_weight(group[0]) for group in ordered]
    else:
        raw = _groups(cleaned)
        ordered = _collapse_catalog(raw[:256])
        weights = [_weight(group[0]) for group in ordered]
        keywords = [group[0] for group in raw]
        english_terms = [(word, _weight(word)) for word in keywords]
    if not ordered and not clause_phrases:
        return QueryTerms([], {}, [])
    expanded = {}
    proximity = {}
    for group in ordered:
        term = group[0]
        if chinese and _HAN.fullmatch(term):
            fine = tuple(word for word in group[1:] if len(word) > 1)
            group[1:] = fine
            if len(fine) > 1:
                proximity[term] = fine
        expanded[term] = _aliases(group)
    if not chinese:
        keywords.extend(_phrase_text(alias) for group in ordered for alias in expanded[group[0]])
    return build_query_terms(
        ordered, weights,
        ranking=[(word, _weight(word)) for word in keywords],
        synonyms=expanded, chinese=chinese, proximity=proximity, clauses=tuple(clauses),
        whole_phrases=whole_phrases, clause_aliases=clause_aliases,
        clause_phrases=clause_phrases, english_terms=english_terms,
    )


@lru_cache(maxsize=1)
def _wordnet_phrase_spans() -> dict[str, dict[int, set[tuple[str, ...]]]]:
    result: dict[str, dict[int, set[tuple[str, ...]]]] = {}
    for alias in multiword_lemmas():
        words = _primary(alias)
        # 两词短语已有相邻词键，补充 WordNet 真正用到的更长短语。
        if len(words) > 2:
            result.setdefault(words[0], {}).setdefault(len(words), set()).add(words)
    return result


def _position_stream(words: Sequence[str]) -> str:
    """用 ASCII 词键保留词序，交给 PostgreSQL 匹配词距。"""
    positions: dict[str, list[str]] = defaultdict(list)
    for index, word in enumerate(words[:16383], 1):
        key = position_key(word)
        if len(key) < 2047:
            positions[key].append(str(index))
    return " ".join(f"{key}:{','.join(offsets)}" for key, offsets in positions.items())


@dataclass(frozen=True)
class _FieldTerms:
    primary: set[str]
    fine: set[str]
    positions: list[str]
    fine_positions: list[str]
    ranking_tokens: list[tuple[str, ...]]


def _field_terms(fields: Sequence[str]) -> _FieldTerms:
    primary: set[str] = set()
    fine: set[str] = set()
    primary_positions: list[str] = []
    fine_positions: list[str] = []
    ranking_tokens: list[tuple[str, ...]] = []
    for value in fields:
        positional = _groups(value, keep_stop=True)
        groups = [group for group in positional if group[0] not in _STOP]
        # 上游按完整词序计算相邻词组，停用词只从召回词项中排除。
        ordered = tuple(group[0] for group in positional)
        ranking_tokens.append(ordered)
        if positional:
            primary_positions.append(_position_stream(ordered))
            fine_positions.append(_position_stream([
                word for group in positional
                for word in (group[1:] if len(group) > 1 and _HAN.fullmatch(group[0]) else group[:1])
            ]))
        words = tuple(group[0] for group in groups)
        primary.update(words)
        primary.update(_pairs(groups))
        primary.update(_pairs(_collapse_catalog(groups)))
        primary.update(phrase_key(match) for start in range(len(words)) for match in _catalog_matches(words, start))
        if any(word.isascii() and word.isalpha() for word in words):
            wordnet = _wordnet_phrase_spans()
            for start, word in enumerate(words):
                for length, entries in wordnet.get(word, {}).items():
                    match = words[start:start + length]
                    if match in entries:
                        primary.add(phrase_key(match))
        fine.update(term for group in groups for term in group[1:] if term not in _STOP)
    return _FieldTerms(primary, fine, primary_positions, fine_positions, ranking_tokens)


@dataclass(frozen=True)
class IndexedTerms:
    terms: list[str]
    boosted: list[str]
    fields: dict[str, list[str]]


def search_fields(
    body: str, title: str, headings: list[str], header: str = "", *,
    row_fields: Sequence[str] = (),
) -> IndexedTerms:
    titles = _field_terms([
        re.sub(r"\.[a-zA-Z]+$", "", title), *headings,
    ])
    important = _field_terms([header])
    content = _field_terms([body, *row_fields])
    boosted = titles.primary | titles.fine | important.primary | important.fine
    # 上游最终排序先对正文词去重，再附加标题和重要词。表格的列值组合只用于召回。
    title_words = tuple(chain.from_iterable(titles.ranking_tokens))
    ranking = ranking_keys([
        *dict.fromkeys(content.ranking_tokens[0]),
        *(title_words * 2),
        *(important.ranking_tokens[0] * 5),
    ])
    return IndexedTerms(
        sorted(boosted | content.primary | content.fine), sorted(boosted),
        {
            "title": sorted(titles.primary), "title_fine": sorted(titles.fine),
            "important": sorted(important.primary | important.fine),
            "content": sorted(content.primary), "content_fine": sorted(content.fine),
            "title_positions": titles.positions, "title_fine_positions": titles.fine_positions,
            "important_positions": [*important.positions, *important.fine_positions],
            "content_positions": content.positions, "content_fine_positions": content.fine_positions,
            "ranking": sorted(ranking),
        },
    )
