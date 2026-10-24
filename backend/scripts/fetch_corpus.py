"""抓取评测语料：维基文库法律法规正文 → testdata/corpus/ 下的 Markdown。

法律法规为官方文件，依《著作权法》第五条不适用著作权保护；来源与抓取日期
写入各文件首段。仅用于本地评测（用户授权的网络抓取），改动语料清单后重跑：
    python scripts/fetch_corpus.py
"""

from __future__ import annotations

import json
import re
import sys
import urllib.parse
import urllib.request
from pathlib import Path

API = "https://zh.wikisource.org/w/api.php"
OUT_DIR = Path(__file__).resolve().parents[1] / "testdata" / "corpus"
USER_AGENT = "knowledge-service-corpus/0.1 (self-made eval data fetch)"

# 目标语料：文件名 → 候选页面标题（取第一个存在的）
TARGETS: dict[str, list[str]] = {
    "法律-劳动法.md": ["中华人民共和国劳动法 (2018年)"],
    "法律-劳动合同法.md": ["中华人民共和国劳动合同法 (2012年)"],
    "法律-数据安全法.md": ["中华人民共和国数据安全法", "中华人民共和国数据安全法 (2021年)"],
    "法律-网络安全法.md": ["中华人民共和国网络安全法", "中华人民共和国网络安全法 (2016年)"],
    "法规-住房公积金管理条例.md": [
        "住房公积金管理条例",
        "住房公积金管理条例 (2019年)",
        "住房公积金管理条例 (2017年)",
    ],
}

_ARTICLE = re.compile(r"^第[一二三四五六七八九十百零]+[条章]")


def fetch_wikitext(title: str) -> str:
    query = urllib.parse.urlencode(
        {"action": "parse", "page": title, "format": "json", "prop": "wikitext"}
    )
    request = urllib.request.Request(f"{API}?{query}", headers={"User-Agent": USER_AGENT})
    last_error: Exception = TimeoutError(title)
    for _ in range(3):
        try:
            with urllib.request.urlopen(request, timeout=40) as response:
                payload = json.load(response)
            return payload["parse"]["wikitext"]["*"]
        except KeyError:
            raise  # 页面不存在，不重试
        except Exception as exc:  # noqa: BLE001 - 网络抖动重试
            last_error = exc
    raise last_error


def clean(wikitext: str) -> str:
    """wikitext → 以「条」为段落的纯文本；条内续段并入本条。"""
    lines: list[str] = []
    for raw in wikitext.splitlines():
        line = raw.strip()
        if not line:
            lines.append("")
            continue
        line = line.replace("{{gap}}", "")
        if line.startswith(("{{", "|", "}}")):
            continue  # 模板与信息框
        if line.startswith("==") and line.endswith("=="):
            heading = line.strip("= ").strip()
            lines.append(f"## {heading}")
            continue
        line = re.sub(r"\[\[[^\]|]*\|([^\]]*)\]\]", r"\1", line)  # [[a|b]] → b
        line = re.sub(r"\[\[([^\]]*)\]\]", r"\1", line)  # [[a]] → a
        line = line.replace("'''", "").replace("''", "")
        line = re.sub(r"<[^>]+>", "", line)  # HTML 标签
        if not line.strip():
            lines.append("")
            continue
        lines.append(line.strip())

    # 合并条内续段：不以「第X条/章」开头的正文段并入上一段，每条自成一段
    merged: list[str] = []
    for line in lines:
        if not line:
            continue
        if merged and not line.startswith("#") and not _ARTICLE.match(line):
            merged[-1] += line
        else:
            merged.append(line)

    body = "\n\n".join(merged).strip()
    # 只保留从第一个章节/条文标题开始的部分（去掉页首残余说明）
    match = re.search(r"^(## |第[一二三四五六七八九十百零]+[条章])", body, re.MULTILINE)
    return body[match.start() :] if match else body


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    failures: list[str] = []
    for filename, candidates in TARGETS.items():
        text = ""
        used = ""
        for title in candidates:
            try:
                wikitext = fetch_wikitext(title)
            except Exception as exc:  # noqa: BLE001 - 网络候选逐个尝试
                print(f"  [skip] {title}: {exc}")
                continue
            candidate = clean(wikitext)
            if len(candidate) > 3000 and candidate.count("条") >= 20:
                text, used = candidate, title
                break
            print(f"  [thin] {title}: {len(candidate)} chars, not usable")
        if not text:
            failures.append(filename)
            continue

        articles = len(re.findall(r"^第[一二三四五六七八九十百零]+条", text, re.MULTILINE))
        header = (
            f"# {used.replace(' (', '（').replace(')', '）')}（评测语料）\n\n"
            "（语料来源：维基文库，抓取于测试环境；法律法规为官方文件，"
            "依《著作权法》第五条不适用著作权保护。仅用于本项目检索与问答效果评测。）\n\n"
        )
        target = OUT_DIR / filename
        target.write_text(header + text + "\n", encoding="utf-8", newline="\n")
        print(f"[ok] {filename} <- {used}: {len(text)} 字符，{articles} 条")
    if failures:
        print(f"[fail] 未获取：{failures}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
