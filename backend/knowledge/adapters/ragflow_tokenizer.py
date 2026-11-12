"""Infinity SDK 0.7.0.dev2 的分词核心。

保留原算法，词典改用支持 Windows 的 MARISA 索引。来源见 ragflow_data/README.md。
"""

#
#  Copyright 2025 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#

from __future__ import annotations

import copy
import math
import re
from functools import lru_cache
from pathlib import Path
from threading import Lock

from hanziconv import HanziConv
from marisa_trie import RecordTrie, Trie
from nltk import word_tokenize
from nltk.data import find
from nltk.stem import SnowballStemmer

from knowledge.adapters.ragflow_wordnet import lemmatize_english
from knowledge.domain.errors import KeywordIndexUnavailable


class _Dictionary:
    def __init__(self, path: Path) -> None:
        tags: dict[str, int] = {}
        entries: dict[str, tuple[int, int]] = {}
        with path.open(encoding="utf-8") as source:
            for line in source:
                word, frequency, tag = re.split(r"[ \t]", line.rstrip("\r\n"))
                word = word.lower()
                score = int(math.log(float(frequency) / 1_000_000) + 0.5)
                if word not in entries or score > entries[word][0]:
                    entries[word] = (score, tags.setdefault(tag, len(tags)))
        self._tags = list(tags)
        self._forward = RecordTrie("<iH", entries.items())
        self._reverse = Trie(word[::-1] for word in entries)

    def __contains__(self, key: str) -> bool:
        return key in self._forward

    def __getitem__(self, key: str) -> tuple[int, str]:
        score, tag = self._forward[key][0]
        return score, self._tags[tag]

    def has_keys_with_prefix(self, key: str) -> bool:
        if key.startswith("DD"):
            return next(self._reverse.iterkeys(key[2:]), None) is not None
        return next(self._forward.iterkeys(key), None) is not None


class _Lemmatizer:
    lemmatize = staticmethod(lemmatize_english)


class RagTokenizer:
    def __init__(self, path: Path) -> None:
        self.DENOMINATOR = 1_000_000
        self.stemmer = SnowballStemmer("english")
        self.lemmatizer = _Lemmatizer()
        self.trie_ = _Dictionary(path)
        self.SPLIT_CHAR = r"([ ,\.<>/?;:'\[\]\\`!@#$%^&*\(\)\{\}\|_+=《》，。？、；‘’：“”【】~！￥%……（）——-]+|[a-zA-Z0-9,\.-]+)"

    def key_(self, line: str) -> str:
        return line.lower()

    def rkey_(self, line: str) -> str:
        return "DD" + line[::-1].lower()

    def _strQ2B(self, ustring):
        """Convert full-width characters to half-width characters"""
        rstring = ""
        for uchar in ustring:
            inside_code = ord(uchar)
            if inside_code == 0x3000:
                inside_code = 0x0020
            else:
                inside_code -= 0xFEE0
            if inside_code < 0x0020 or inside_code > 0x7E:  # After the conversion, if it's not a half-width character, return the original character.
                rstring += uchar
            else:
                rstring += chr(inside_code)
        return rstring

    def _tradi2simp(self, line):
        return HanziConv.toSimplified(line)

    def dfs_(self, chars, s, preTks, tkslist, _depth=0, _memo=None):
        if _memo is None:
            _memo = {}
        MAX_DEPTH = 10
        if _depth > MAX_DEPTH:
            if s < len(chars):
                copy_pretks = copy.deepcopy(preTks)
                remaining = "".join(chars[s:])
                copy_pretks.append((remaining, (-12, "")))
                tkslist.append(copy_pretks)
            return s

        state_key = (s, tuple(tk[0] for tk in preTks)) if preTks else (s, None)
        if state_key in _memo:
            return _memo[state_key]

        res = s
        if s >= len(chars):
            tkslist.append(preTks)
            _memo[state_key] = s
            return s
        if s < len(chars) - 4:
            is_repetitive = True
            char_to_check = chars[s]
            for i in range(1, 5):
                if s + i >= len(chars) or chars[s + i] != char_to_check:
                    is_repetitive = False
                    break
            if is_repetitive:
                end = s
                while end < len(chars) and chars[end] == char_to_check:
                    end += 1
                mid = s + min(10, end - s)
                t = "".join(chars[s:mid])
                k = self.key_(t)
                copy_pretks = copy.deepcopy(preTks)
                if k in self.trie_:
                    copy_pretks.append((t, self.trie_[k]))
                else:
                    copy_pretks.append((t, (-12, "")))
                next_res = self.dfs_(chars, mid, copy_pretks, tkslist, _depth + 1, _memo)
                res = max(res, next_res)
                _memo[state_key] = res
                return res

        S = s + 1
        if s + 2 <= len(chars):
            t1 = "".join(chars[s: s + 1])
            t2 = "".join(chars[s: s + 2])
            if self.trie_.has_keys_with_prefix(self.key_(t1)) and not self.trie_.has_keys_with_prefix(self.key_(t2)):
                S = s + 2
        if len(preTks) > 2 and len(preTks[-1][0]) == 1 and len(preTks[-2][0]) == 1 and len(preTks[-3][0]) == 1:
            t1 = preTks[-1][0] + "".join(chars[s: s + 1])
            if self.trie_.has_keys_with_prefix(self.key_(t1)):
                S = s + 2

        for e in range(S, len(chars) + 1):
            t = "".join(chars[s:e])
            k = self.key_(t)
            if e > s + 1 and not self.trie_.has_keys_with_prefix(k):
                break
            if k in self.trie_:
                pretks = copy.deepcopy(preTks)
                pretks.append((t, self.trie_[k]))
                res = max(res, self.dfs_(chars, e, pretks, tkslist, _depth + 1, _memo))

        if res > s:
            _memo[state_key] = res
            return res

        t = "".join(chars[s: s + 1])
        k = self.key_(t)
        copy_pretks = copy.deepcopy(preTks)
        if k in self.trie_:
            copy_pretks.append((t, self.trie_[k]))
        else:
            copy_pretks.append((t, (-12, "")))
        result = self.dfs_(chars, s + 1, copy_pretks, tkslist, _depth + 1, _memo)
        _memo[state_key] = result
        return result

    def freq(self, tk):
        k = self.key_(tk)
        if k not in self.trie_:
            return 0
        return int(math.exp(self.trie_[k][0]) * self.DENOMINATOR + 0.5)

    def tag(self, tk):
        k = self.key_(tk)
        if k not in self.trie_:
            return ""
        return self.trie_[k][1]

    def score_(self, tfts):
        B = 30
        F, L, tks = 0, 0.0, []
        for tk, (freq, tag) in tfts:
            F += freq
            L += 0 if len(tk) < 2 else 1
            tks.append(tk)
        # F /= len(tks)
        L /= len(tks)
        return tks, B / len(tks) + L + F

    def _sort_tokens(self, tkslist):
        res = []
        for tfts in tkslist:
            tks, s = self.score_(tfts)
            res.append((tks, s))
        return sorted(res, key=lambda x: x[1], reverse=True)

    def merge_(self, tks):
        # if split chars is part of token
        res = []
        tks = re.sub(r"[ ]+", " ", tks).split()
        s = 0
        while True:
            if s >= len(tks):
                break
            E = s + 1
            for e in range(s + 2, min(len(tks) + 2, s + 6)):
                tk = "".join(tks[s:e])
                if re.search(self.SPLIT_CHAR, tk) and self.freq(tk):
                    E = e
            res.append("".join(tks[s:E]))
            s = E

        return " ".join(res)

    def _max_forward(self, line):
        res = []
        s = 0
        while s < len(line):
            e = s + 1
            t = line[s:e]
            while e < len(line) and self.trie_.has_keys_with_prefix(self.key_(t)):
                e += 1
                t = line[s:e]

            while e - 1 > s and self.key_(t) not in self.trie_:
                e -= 1
                t = line[s:e]

            if self.key_(t) in self.trie_:
                res.append((t, self.trie_[self.key_(t)]))
            else:
                res.append((t, (0, "")))

            s = e

        return self.score_(res)

    def _max_backward(self, line):
        res = []
        s = len(line) - 1
        while s >= 0:
            e = s + 1
            t = line[s:e]
            while s > 0 and self.trie_.has_keys_with_prefix(self.rkey_(t)):
                s -= 1
                t = line[s:e]

            while s + 1 < e and self.key_(t) not in self.trie_:
                s += 1
                t = line[s:e]

            if self.key_(t) in self.trie_:
                res.append((t, self.trie_[self.key_(t)]))
            else:
                res.append((t, (0, "")))

            s -= 1

        return self.score_(res[::-1])

    def english_normalize_(self, tks):
        return [self.stemmer.stem(self.lemmatizer.lemmatize(t)) if re.match(r"[a-zA-Z_-]+$", t) else t for t in tks]

    def _split_by_lang(self, line):
        txt_lang_pairs = []
        arr = re.split(self.SPLIT_CHAR, line)
        for a in arr:
            if not a:
                continue
            s = 0
            e = s + 1
            zh = is_chinese(a[s])
            while e < len(a):
                _zh = is_chinese(a[e])
                if _zh == zh:
                    e += 1
                    continue
                txt_lang_pairs.append((a[s:e], zh))
                s = e
                e = s + 1
                zh = _zh
            if s >= len(a):
                continue
            txt_lang_pairs.append((a[s:e], zh))
        return txt_lang_pairs

    def tokenize(self, line: str) -> str:
        line = re.sub(r"\W+", " ", line)
        line = self._strQ2B(line).lower()
        line = self._tradi2simp(line)

        arr = self._split_by_lang(line)
        res = []
        for L, lang in arr:
            if not lang:
                res.extend([self.stemmer.stem(self.lemmatizer.lemmatize(t)) for t in word_tokenize(L, preserve_line=True)])
                continue
            if len(L) < 2 or re.match(r"[a-z\.-]+$", L) or re.match(r"[0-9\.-]+$", L):
                res.append(L)
                continue

            # use maxforward for the first time
            tks, _ = self._max_forward(L)
            tks1, _ = self._max_backward(L)

            i, j, _i, _j = 0, 0, 0, 0
            same = 0
            while i + same < len(tks1) and j + same < len(tks) and tks1[i + same] == tks[j + same]:
                same += 1
            if same > 0:
                res.append(" ".join(tks[j: j + same]))
            _i = i + same
            _j = j + same
            j = _j + 1
            i = _i + 1

            while i < len(tks1) and j < len(tks):
                tk1, tk = "".join(tks1[_i:i]), "".join(tks[_j:j])
                if tk1 != tk:
                    if len(tk1) > len(tk):
                        j += 1
                    else:
                        i += 1
                    continue

                if tks1[i] != tks[j]:
                    i += 1
                    j += 1
                    continue
                # backward tokens from_i to i are different from forward tokens from _j to j.
                tkslist: list[list[tuple[str, tuple[int, str]]]] = []
                self.dfs_("".join(tks[_j:j]), 0, [], tkslist)
                res.append(" ".join(self._sort_tokens(tkslist)[0][0]))

                same = 1
                while i + same < len(tks1) and j + same < len(tks) and tks1[i + same] == tks[j + same]:
                    same += 1
                res.append(" ".join(tks[j: j + same]))
                _i = i + same
                _j = j + same
                j = _j + 1
                i = _i + 1

            if _i < len(tks1):
                assert _j < len(tks)
                assert "".join(tks1[_i:]) == "".join(tks[_j:])
                tkslist = []
                self.dfs_("".join(tks[_j:]), 0, [], tkslist)
                res.append(" ".join(self._sort_tokens(tkslist)[0][0]))

        return self.merge_(" ".join(res))

    def fine_grained_tokenize(self, tks: str) -> str:
        tokens = tks.split()
        zh_num = len([1 for c in tokens if c and is_chinese(c[0])])
        if zh_num < len(tokens) * 0.2:
            res = []
            for tk in tokens:
                res.extend(tk.split("/"))
            return " ".join(res)

        res = []
        for tk in tokens:
            if len(tk) < 3 or re.match(r"[0-9,\.-]+$", tk):
                res.append(tk)
                continue
            tkslist = []
            if len(tk) > 10:
                tkslist.append(tk)
            else:
                self.dfs_(tk, 0, [], tkslist)
            if len(tkslist) < 2:
                res.append(tk)
                continue
            stk = self._sort_tokens(tkslist)[1][0]
            if len(stk) == len(tk):
                stk = tk
            else:
                if re.match(r"[a-z\.-]+$", tk):
                    for t in stk:
                        if len(t) < 3:
                            stk = tk
                            break
                    else:
                        stk = " ".join(stk)
                else:
                    stk = " ".join(stk)

            res.append(stk)

        return " ".join(self.english_normalize_(res))


def is_chinese(s: str) -> bool:
    return "\u4e00" <= s <= "\u9fa5"

_LOCK = Lock()


@lru_cache(maxsize=1)
def _load_tokenizer() -> RagTokenizer:
    try:
        path = Path(str(find("ragflow/huqie.txt")))
    except LookupError as exc:
        raise KeywordIndexUnavailable("缺少本地 Infinity huqie 词典，请先准备 RAGFlow 资源") from exc
    return RagTokenizer(path)


def tokenizer() -> RagTokenizer:
    with _LOCK:
        return _load_tokenizer()
