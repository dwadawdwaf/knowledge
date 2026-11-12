"""RAGFlow v0.24.0 FulltextQueryer.question，保留原生 Lucene 查询表达式。

Copyright 2024 The InfiniFlow Authors. Apache-2.0.
只替换资源及分词装配；不引入上游平台、Redis 和生成链路。
"""
from __future__ import annotations

import json
import re
import unicodedata
from functools import lru_cache
from pathlib import Path

from hanziconv import HanziConv

from knowledge.adapters.ragflow_text import (
    _CLAUSE_STOP,
    _clause_words,
    _weight,
)
from knowledge.adapters.ragflow_tokenizer import tokenizer
from knowledge.adapters.ragflow_wordnet import synonyms_for


class _Weights:
    stop_words = _CLAUSE_STOP
    def weights(self, tks, preprocess=True):
        terms = [t for tk in tks for t in self.token_merge(self.pretoken(tk, True))] if preprocess else list(tks)
        values = [(t, _weight(t)) for t in terms]
        total = sum(w for _, w in values)
        return [(t, w / total) for t, w in values] if total else []
    def split(self, text):
        return _clause_words(re.sub(r"[ \t]+", " ", text).split())
    def pretoken(self, txt, num=False, stpwd=True):
        patt = [
            r"[~—\t @#%!<>,\.\?\":;'\{\}\[\]_=\(\)\|，。？》•●○↓《；‘’：“”【¥ 】…￥！、·（）×`&\\/「」\\]"
        ]
        rewt: list[tuple[str, str]] = []
        for p, r in rewt:
            txt = re.sub(p, r, txt)

        res = []
        for t in tokenizer().tokenize(txt).split():
            tk = t
            if (stpwd and tk in self.stop_words) or (
                    re.match(r"[0-9]$", tk) and not num):
                continue
            for p in patt:
                if re.match(p, t):
                    tk = "#"
                    break
            # tk = re.sub(r"([\+\\-])", r"\\\1", tk)
            if tk != "#" and tk:
                res.append(tk)
        return res

    def token_merge(self, tks):
        def one_term(t):
            return len(t) == 1 or re.match(r"[0-9a-z]{1,2}$", t)

        res, i = [], 0
        while i < len(tks):
            j = i
            if i == 0 and one_term(tks[i]) and len(
                    tks) > 1 and (len(tks[i + 1]) > 1 and not re.match(r"[0-9a-zA-Z]", tks[i + 1])):  # 多 工位
                res.append(" ".join(tks[0:2]))
                i = 2
                continue

            while j < len(
                    tks) and tks[j] and tks[j] not in self.stop_words and one_term(tks[j]):
                j += 1
            if j - i > 1:
                if j - i < 5:
                    res.append(" ".join(tks[i:j]))
                    i = j
                else:
                    res.append(" ".join(tks[i:i + 2]))
                    i = i + 2
            else:
                if len(tks[i]) > 0:
                    res.append(tks[i])
                i += 1
        return [t for t in res if t]


class _Synonyms:
    @staticmethod
    @lru_cache(maxsize=8192)
    def lookup(term):
        source = _dictionary()
        found = source.get(re.sub(r"[ \t]+", " ", term.strip().lower()), [])
        return ([found] if isinstance(found, str) else found)[:8] if found else list(synonyms_for(term))[:8]
@lru_cache(maxsize=1)
def _dictionary():
    raw = json.loads(Path(__file__).with_name('ragflow_data').joinpath('synonym.json').read_text(encoding='utf-8'))
    return {k.lower():v for k,v in raw.items()}

class QueryBase:

    @staticmethod
    def is_chinese(line):
        arr = re.split(r"[ \t]+", line)
        if len(arr) <= 3:
            return True
        e = 0
        for t in arr:
            if not re.match(r"[a-zA-Z]+$", t):
                e += 1
        return e * 1.0 / len(arr) >= 0.7

    @staticmethod
    def sub_special_char(line):
        return re.sub(r"([:\{\}/\[\]\-\*\"\(\)\|\+~\^])", r"\\\1", line).strip()

    @staticmethod
    def rmWWW(txt):
        patts = [
            (
                r"是*(怎么办|什么样的|哪家|一下|那家|请问|啥样|咋样了|什么时候|何时|何地|何人|是否|是不是|多少|哪里|怎么|哪儿|怎么样|如何|哪些|是啥|啥是|啊|吗|呢|吧|咋|什么|有没有|呀|谁|哪位|哪个)是*",
                "",
            ),
            (r"(^| )(what|who|how|which|where|why)('re|'s)? ", " "),
            (
                r"(^| )('s|'re|is|are|were|was|do|does|did|don't|doesn't|didn't|has|have|be|there|you|me|your|my|mine|just|please|may|i|should|would|wouldn't|will|won't|done|go|for|with|so|the|a|an|by|i'm|it's|he's|she's|they|they're|you're|as|by|on|in|at|up|out|down|of|to|or|and|if) ",
                " ")
        ]
        otxt = txt
        for r, p in patts:
            txt = re.sub(r, p, txt, flags=re.IGNORECASE)
        if not txt:
            txt = otxt
        return txt

    @staticmethod
    def add_space_between_eng_zh(txt):
        # (ENG/ENG+NUM) + ZH
        txt = re.sub(r'([A-Za-z]+[0-9]+)([\u4e00-\u9fa5]+)', r'\1 \2', txt)
        # ENG + ZH
        txt = re.sub(r'([A-Za-z])([\u4e00-\u9fa5]+)', r'\1 \2', txt)
        # ZH + (ENG/ENG+NUM)
        txt = re.sub(r'([\u4e00-\u9fa5]+)([A-Za-z]+[0-9]+)', r'\1 \2', txt)
        txt = re.sub(r'([\u4e00-\u9fa5]+)([A-Za-z])', r'\1 \2', txt)
        return txt



class NativeQuery(QueryBase):
    def __init__(self):
        self.tw = _Weights()
        self.syn = _Synonyms()
        self.query_fields = ["title_tks^10", "title_sm_tks^5", "important_kwd^30", "important_tks^20", "question_tks^20", "content_ltks^2", "content_sm_ltks"]
    def question(self, txt, tbl="qa", min_match: float = 0.6):
        original_query = txt
        txt = self.add_space_between_eng_zh(txt)
        txt = re.sub(
            r"[ :|\r\n\t,，。？?/`!！&^%%()\[\]{}<>]+",
            " ",
            HanziConv.toSimplified(unicodedata.normalize("NFKC", txt.lower())),
        ).strip()
        otxt = txt
        txt = self.rmWWW(txt)

        if not self.is_chinese(txt):
            txt = self.rmWWW(txt)
            tks = tokenizer().tokenize(txt).split()
            keywords = [t for t in tks if t]
            tks_w = self.tw.weights(tks, preprocess=False)
            tks_w = [(re.sub(r"[ \\\"'^]", "", tk), w) for tk, w in tks_w]
            tks_w = [(re.sub(r"^[\+-]", "", tk), w) for tk, w in tks_w if tk]
            tks_w = [(tk.strip(), w) for tk, w in tks_w if tk.strip()]
            syns = []
            for tk, w in tks_w[:256]:
                syn = [tokenizer().tokenize(s) for s in self.syn.lookup(tk)]
                keywords.extend(syn)
                syn = [f"\"{s}\"^{w / 4.:.4f}" for s in syn if s.strip()]
                syns.append(" ".join(syn))

            q = [f"({tk}^{w:.4f}" + f" {syn})" for (tk, w), syn in zip(tks_w, syns) if
                 tk and not re.match(r"[.^+\(\)-]", tk)]
            for i in range(1, len(tks_w)):
                left, right = tks_w[i - 1][0].strip(), tks_w[i][0].strip()
                if not left or not right:
                    continue
                q.append(
                    f'"{tks_w[i - 1][0]} {tks_w[i][0]}"^{max(tks_w[i - 1][1], tks_w[i][1]) * 2:.4f}'
                )
            if not q:
                q.append(txt)
            query = " ".join(q)
            return _match(
                self.query_fields, query, 100, {"original_query": original_query}
            ), keywords

        def need_fine_grained_tokenize(tk):
            if len(tk) < 3:
                return False
            return not re.match(r"[0-9a-z\.\+#_\*-]+$", tk)

        txt = self.rmWWW(txt)
        qs, keywords = [], []
        for tt in self.tw.split(txt)[:256]:  # .split():
            if not tt:
                continue
            keywords.append(tt)
            twts = self.tw.weights([tt])
            syns = self.syn.lookup(tt)
            if syns and len(keywords) < 32:
                keywords.extend(syns)
            tms = []
            for tk, w in sorted(twts, key=lambda x: x[1] * -1):
                sm = (
                    tokenizer().fine_grained_tokenize(tk).split()
                    if need_fine_grained_tokenize(tk)
                    else []
                )
                sm = [
                    re.sub(
                        r"[ ,\./;'\[\]\\`~!@#$%\^&\*\(\)=\+_<>\?:\"\{\}\|，。；‘’【】、！￥……（）——《》？：“”-]+",
                        "",
                        m,
                    )
                    for m in sm
                ]
                sm = [self.sub_special_char(m) for m in sm if len(m) > 1]
                sm = [m for m in sm if len(m) > 1]

                if len(keywords) < 32:
                    keywords.append(re.sub(r"[ \\\"']+", "", tk))
                    keywords.extend(sm)

                tk_syns = self.syn.lookup(tk)
                tk_syns = [self.sub_special_char(s) for s in tk_syns]
                if len(keywords) < 32:
                    keywords.extend([s for s in tk_syns if s])
                tk_syns = [tokenizer().fine_grained_tokenize(s) for s in tk_syns if s]
                tk_syns = [f"\"{s}\"" if s.find(" ") > 0 else s for s in tk_syns]

                if len(keywords) >= 32:
                    break

                tk = self.sub_special_char(tk)
                if tk.find(" ") > 0:
                    tk = f'"{tk}"'
                if tk_syns:
                    tk = f"({tk} OR (%s)^0.2)" % " ".join(tk_syns)
                if sm:
                    tk = f'{tk} OR "%s" OR ("%s"~2)^0.5' % (" ".join(sm), " ".join(sm))
                if tk.strip():
                    tms.append((tk, w))

            clause_expression = " ".join([f"({t})^{w}" for t, w in tms])

            if len(twts) > 1:
                clause_expression += f' ("{tokenizer().tokenize(tt)}"~2)^1.5'

            alias_expression = " OR ".join(
                [
                    f'"{tokenizer().tokenize(self.sub_special_char(s))}"'
                    for s in syns
                ]
            )
            if alias_expression and clause_expression:
                clause_expression = f"({clause_expression})^5 OR ({alias_expression})^0.7"

            qs.append(clause_expression)

        if qs:
            query = " OR ".join([f"({t})" for t in qs if t])
            if not query:
                query = otxt
            return _match(
                self.query_fields, query, 100, {"minimum_should_match": min_match, "original_query": original_query}
            ), keywords
        return None, keywords


def _match(fields, query, topn, options):
    return {"query_string": {"fields": fields, "type": "best_fields", "query": query, "minimum_should_match": str(int(options.get("minimum_should_match", 0) * 100)) + "%"}}

def native_query(text: str, minimum: float = .3) -> dict | None:
    return NativeQuery().question(text, min_match=minimum)[0]

@lru_cache(maxsize=512)
def native_features(text: str) -> dict[str, float]:
    # 与上游 token_similarity 一致：单词0.4，相邻拼接词组0.6。
    _, keywords = NativeQuery().question(text)
    weighted = _Weights().weights(keywords, preprocess=False)
    result: dict[str, float] = {}
    for i, (token, weight) in enumerate(weighted):
        result[token] = result.get(token, 0) + weight * .4
        if i + 1 < len(weighted):
            following, next_weight = weighted[i + 1]
            key = token + following
            result[key] = result.get(key, 0) + max(weight, next_weight) * .6
    return result
