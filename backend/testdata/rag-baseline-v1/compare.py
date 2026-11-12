#!/usr/bin/env python3
"""对比两次 rag-baseline-v1 运行：同一语料、同一分块，只切换检索组件。

用法：
    python compare.py 旧/summary.json 新/summary.json [--labels PostgreSQL,OpenSearch] [--md 输出.md]

输出总体指标、延迟与 format/lang/type 分片的 Hit@5 差值；正数表示新组件更好。
"""

from __future__ import annotations

import argparse
import json
import pathlib
from typing import Any


def load(path: str) -> dict[str, Any]:
    return json.loads(pathlib.Path(path).read_text(encoding="utf-8"))


def signed(value: float | None, digits: int = 4) -> str:
    if value is None:
        return "-"
    return f"{value:+.{digits}f}"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("base")
    parser.add_argument("candidate")
    parser.add_argument("--labels", default="基线,新组件")
    parser.add_argument("--md", default=None)
    args = parser.parse_args()
    base, candidate = load(args.base), load(args.candidate)
    base_label, new_label = [item.strip() for item in args.labels.split(",")][:2]
    modes = [mode for mode in candidate["modes"] if mode in base["modes"]]
    lines = [
        "# 检索组件对比",
        "",
        f"- {base_label}：{base.get('started_at')} / {base.get('search_engine', 'postgresql')}",
        f"- {new_label}：{candidate.get('started_at')} / {candidate.get('search_engine', 'postgresql')}",
        f"- 语料：{candidate['corpus']['documents']} 份文档、{candidate['corpus']['queries']} 道查询；"
        f"失败文档 {candidate['documents_failed']}、跳过 {candidate['documents_skipped']}、查询错误 {candidate['query_errors']}",
        "",
        "## 总体",
        "",
        f"| 模式 | 指标 | {base_label} | {new_label} | 差值 |",
        "| --- | --- | --- | --- | --- |",
    ]
    for mode in modes:
        old, new = base["modes"][mode], candidate["modes"][mode]
        for key in ("hit@1", "hit@5", "hit@20", "any@5", "mrr"):
            lines.append(f"| {mode} | {key} | {old[key]} | {new[key]} | {signed((new[key] or 0) - (old[key] or 0))} |")
        lines.append(
            f"| {mode} | 无答案有候选 | {old['no_answer_with_candidates']}/{old['no_answer_total']} | "
            f"{new['no_answer_with_candidates']}/{new['no_answer_total']} | "
            f"{new['no_answer_with_candidates'] - old['no_answer_with_candidates']:+d} |"
        )
        lines.append(
            f"| {mode} | P50 ms | {old['latency_ms']['p50']} | {new['latency_ms']['p50']} | "
            f"{signed((new['latency_ms']['p50'] or 0) - (old['latency_ms']['p50'] or 0), 1)} |"
        )
        lines.append(
            f"| {mode} | P95 ms | {old['latency_ms']['p95']} | {new['latency_ms']['p95']} | "
            f"{signed((new['latency_ms']['p95'] or 0) - (old['latency_ms']['p95'] or 0), 1)} |"
        )
        lines.append(
            f"| {mode} | 错误 | {old['errors']} | {new['errors']} | {new['errors'] - old['errors']:+d} |"
        )
    lines += ["", "## 分片 Hit@5", ""]
    for dimension in ("format", "lang", "type"):
        lines += [
            f"### 按 {dimension}",
            "",
            f"| 分组 | 题数 | " + " | ".join(f"{mode} {base_label} → {new_label}" for mode in modes) + " | 差值 |",
            "| --- | --- | " + " | ".join("---" for _ in modes) + " | --- |",
        ]
        keys = sorted(set(base["breakdown"].get(dimension, {})) | set(candidate["breakdown"].get(dimension, {})))
        for key in keys:
            old_group = base["breakdown"].get(dimension, {}).get(key)
            new_group = candidate["breakdown"].get(dimension, {}).get(key)
            if not old_group or not new_group:
                continue
            cells = []
            deltas = []
            for mode in modes:
                old_value = old_group["modes"].get(mode, {}).get("hit@5")
                new_value = new_group["modes"].get(mode, {}).get("hit@5")
                cells.append(f"{old_value} → {new_value}")
                deltas.append(signed((new_value or 0) - (old_value or 0)))
            lines.append(f"| {key} | {new_group['count']} | " + " | ".join(cells) + " | " + " / ".join(deltas) + " |")
        lines.append("")
    text = "\n".join(lines)
    print(text)
    if args.md:
        pathlib.Path(args.md).write_text(text + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()
