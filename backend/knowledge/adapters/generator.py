"""生成适配器。模型只允许输出 sections[].evidence_keys，不产出 ID、路径或 URL。"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.request
from collections.abc import Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from knowledge.config import Settings
from knowledge.domain.entities import Evidence, GeneratedAnswer, GeneratedSection
from knowledge.domain.errors import CitationValidationFailed, ModelTimeout, ModelUnavailable

SYSTEM_PROMPT = (
    "你是知识库问答助手。只能依据给定的证据片段作答，不得使用外部知识。"
    '输出必须是 JSON：{"sections": [{"text": "...", "evidence_keys": ["E1"]}]}。'
    "每个事实性段落都必须给出 evidence_keys；证据不足时返回空 sections。"
    "不得输出文件名、路径、URL 或数据库标识。"
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class _ChatMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    content: str


class _ChatChoice(BaseModel):
    message: _ChatMessage


class _ChatResponse(BaseModel):
    choices: list[_ChatChoice] = Field(min_length=1)


class _Section(BaseModel):
    model_config = ConfigDict(strict=True)
    text: str
    evidence_keys: list[str] = Field(default_factory=list)


class _GeneratedPayload(BaseModel):
    sections: list[_Section]


def build_prompt(question: str, evidence: Sequence[Evidence]) -> str:
    lines = ["证据片段："]
    for item in evidence:
        lines.append(f"[{item.key}] {item.text.strip()}")
    lines.append("")
    lines.append(f"问题：{question}")
    return "\n".join(lines)


class StubGenerator:
    """无模型环境下的确定性实现：直接复述首条证据，用于验证引用与断链逻辑。"""

    revision = "stub"

    def generate(self, question: str, evidence: Sequence[Evidence]) -> GeneratedAnswer:
        if not evidence:
            return GeneratedAnswer(sections=())
        first = evidence[0]
        text = first.text.strip().replace("\n", " ")
        if len(text) > 240:
            text = text[:240] + "…"
        return GeneratedAnswer(
            sections=(GeneratedSection(text=f"依据资料：{text}", evidence_keys=(first.key,)),),
            model_revision=self.revision,
        )


class HttpGenerator:
    """OpenAI 兼容的 /chat/completions 端点。"""

    def __init__(
        self,
        base_url: str,
        model: str,
        timeout: float = 60.0,
        api_key: str | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.revision = model
        self.timeout = timeout
        self.api_key = api_key

    def _post(self, question: str, evidence: Sequence[Evidence]) -> str:
        payload = json.dumps(
            {
                "model": self.model,
                "temperature": 0,
                "messages": [
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": build_prompt(question, evidence)},
                ],
            }
        ).encode("utf-8")
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=payload,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                body = _ChatResponse.model_validate_json(response.read())
        except TimeoutError as exc:  # pragma: no cover - 依赖外部服务
            raise ModelTimeout("生成服务超时") from exc
        except urllib.error.HTTPError as exc:  # pragma: no cover
            raise ModelUnavailable(f"生成服务返回 {exc.code}") from exc
        except urllib.error.URLError as exc:  # pragma: no cover
            raise ModelUnavailable("生成服务不可用") from exc
        except ValidationError as exc:
            raise ModelUnavailable("生成服务响应结构无效") from exc

        return body.choices[0].message.content

    def generate(self, question: str, evidence: Sequence[Evidence]) -> GeneratedAnswer:
        raw = self._post(question, evidence)
        cleaned = _FENCE.sub("", raw).strip()
        try:
            payload = _GeneratedPayload.model_validate_json(cleaned)
        except ValidationError as exc:
            raise CitationValidationFailed("模型输出结构不符合证据约定") from exc

        sections: list[GeneratedSection] = []
        for item in payload.sections:
            text = item.text.strip()
            if not text:
                continue
            keys = tuple(item.evidence_keys)
            sections.append(GeneratedSection(text=text, evidence_keys=keys))

        return GeneratedAnswer(sections=tuple(sections), model_revision=self.revision)


def build_generator(settings: Settings) -> StubGenerator | HttpGenerator:
    if settings.generator_base_url:
        return HttpGenerator(
            base_url=settings.generator_base_url,
            model=settings.generator_model,
            timeout=settings.generator_timeout_seconds,
            api_key=settings.generator_api_key,
        )
    return StubGenerator()
