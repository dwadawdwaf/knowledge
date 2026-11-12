"""使用 RAGFlow 同一 WordNet 数据格式读取英文候选词和短语。"""

from __future__ import annotations

import re
from collections.abc import Iterator
from functools import lru_cache
from threading import Lock

from nltk.corpus import wordnet
from nltk.corpus.reader import WordNetCorpusReader
from nltk.stem import WordNetLemmatizer

from knowledge.domain.errors import KeywordIndexUnavailable

_LOAD_LOCK = Lock()
_LEMMATIZER = WordNetLemmatizer()
_ALPHABETIC = re.compile(r"[a-z]+")


@lru_cache(maxsize=1)
def _corpus() -> WordNetCorpusReader:
    # NLTK 的 LazyCorpusLoader 在首次使用时替换对象，避免两个请求同时加载。
    with _LOAD_LOCK:
        try:
            wordnet.ensure_loaded()
        except LookupError as exc:
            raise KeywordIndexUnavailable("缺少本地 WordNet 词库，请先准备 RAGFlow 资源") from exc
    return wordnet


@lru_cache(maxsize=4096)
def synonyms_for(term: str) -> tuple[str, ...]:
    if not _ALPHABETIC.fullmatch(term):
        return ()
    # 上游取每个 synset 的主词，保持语义顺序以便稳定复现前八项。
    corpus = _corpus()
    with _LOAD_LOCK:
        return tuple(dict.fromkeys(
            lemma for synset in corpus.synsets(term)
            if (lemma := synset.name().split(".")[0].replace("_", " ")) != term
        ))[:8]


@lru_cache(maxsize=8192)
def lemmatize_english(term: str) -> str:
    _corpus()
    with _LOAD_LOCK:
        return _LEMMATIZER.lemmatize(term)


def multiword_lemmas() -> Iterator[str]:
    corpus = _corpus()
    # 每条数据记录的第一个 lemma 就是 synset.name() 的词项。
    for name in ("adj", "adv", "noun", "verb"):
        with corpus.open(f"data.{name}") as entries:
            for line in entries:
                if line and line[0].isdigit():
                    lemma = line.split(maxsplit=5)[4]
                    if "_" in lemma:
                        yield lemma.replace("_", " ")
