"""自制评测的固定标注及透明指标；词面检查不是语义蕴含或业务验收。"""

from __future__ import annotations

import hashlib
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from scripts.local_runtime import BACKEND

ROOT = BACKEND / "testdata" / "evaluation"
FILES = {
    "room": BACKEND / "testdata" / "engineering" / "e2e-room-v1.md",
    "room_updated": BACKEND / "testdata" / "engineering" / "e2e-room-v2.md",
    "expense": ROOT / "expense.md",
    "archive": ROOT / "archive.md",
}


class Fact(BaseModel):
    model_config = ConfigDict(extra="forbid")
    aliases: list[str] = Field(min_length=1)
    document: Literal["room", "expense", "archive"]
    quote: str = Field(min_length=1)


class Case(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: str
    question: str = Field(min_length=1)
    phase: Literal["initial", "updated"] = "initial"
    answerable: bool = True
    facts: list[Fact] = Field(default_factory=list)
    forbidden: list[str] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_facts(self) -> Case:
        if self.answerable != bool(self.facts):
            raise ValueError("可回答用例必须有事实标注，不可回答用例不得伪造支持片段")
        if any(not alias for fact in self.facts for alias in fact.aliases):
            raise ValueError("事实匹配词不能为空")
        return self


class Dataset(BaseModel):
    model_config = ConfigDict(extra="forbid")
    revision: str
    cases: list[Case] = Field(min_length=1, max_length=15)


def load_dataset() -> tuple[Dataset, str]:
    raw = (ROOT / "cases.json").read_bytes()
    dataset = Dataset.model_validate_json(raw)
    if len({case.id for case in dataset.cases}) != len(dataset.cases):
        raise ValueError("用例 ID 不得重复")
    digest = hashlib.sha256(raw)
    for key, path in sorted(FILES.items()):
        digest.update(key.encode())
        digest.update(path.read_bytes())
    for case in dataset.cases:
        for fact in case.facts:
            key = (
                "room_updated"
                if case.phase == "updated" and fact.document == "room"
                else fact.document
            )
            if fact.quote not in FILES[key].read_text(encoding="utf-8"):
                raise ValueError(f"{case.id} 支持片段不在自制源文件中")
    return dataset, digest.hexdigest()


def retrieval_metrics(case: Case, hits: list[dict], sources: dict[str, dict]) -> dict:
    ranks = []
    for fact in case.facts:
        source = sources[fact.document]
        ranks.append(
            next(
                (
                    rank
                    for rank, hit in enumerate(hits, 1)
                    if hit["document_id"] == source["document_id"]
                    and hit["version_id"] == source["version_id"]
                    and fact.quote in hit["excerpt"]
                ),
                None,
            )
        )
    found = [rank for rank in ranks if rank is not None]
    return {
        "hit_count": len(hits),
        "gold_fact_recall_at_5": len(found) / len(ranks) if ranks else None,
        "first_relevant_rank": min(found) if found else None,
        "reciprocal_rank": 1 / min(found) if found else (0 if ranks else None),
    }


def answer_metrics(
    case: Case, answer: str, status: str, cited: list[dict], sources: dict[str, dict]
) -> dict:
    matched = [any(alias in answer for alias in fact.aliases) for fact in case.facts]
    supported = [
        any(
            chunk["document_id"] == sources[fact.document]["document_id"]
            and chunk["version_id"] == sources[fact.document]["version_id"]
            and fact.quote in chunk["text"]
            for chunk in cited
        )
        for fact in case.facts
    ]
    return {
        "status_matches_label": status == ("answered" if case.answerable else "no_evidence"),
        "fact_lexical_coverage": sum(matched) / len(matched) if matched else None,
        "gold_support_coverage": sum(supported) / len(supported) if supported else None,
        "forbidden_text_detected": any(text in answer for text in case.forbidden),
        "semantic_support_review": "pending_human_review",
    }
