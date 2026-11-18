"""生成适配器。

两条调用约定并存，都与引用校验配套：
- `generate`（非流式）：模型输出 JSON sections，evidence_keys 由服务端映射成引用；
- `stream`（流式）：模型输出纯文本，段落末尾标注 `[E#]`，服务端按标记顺序分配引用。
两者都由服务端分配引用编号，模型不产出 ID、路径或 URL。
"""

from __future__ import annotations

import http.client
import json
import re
import urllib.error
import urllib.request
from collections.abc import Iterator, Sequence

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from knowledge.adapters import outbound
from knowledge.config import Settings
from knowledge.domain.entities import ContextMessage, Evidence, GeneratedAnswer, GeneratedSection
from knowledge.domain.errors import CitationValidationFailed, ModelTimeout, ModelUnavailable

SYSTEM_PROMPT = (
    "你是知识库问答助手。只能依据给定的证据片段作答，不得使用外部知识。"
    '输出必须是 JSON：{"sections": [{"text": "...", "evidence_keys": ["E1"]}]}。'
    "每个事实性段落都必须给出 evidence_keys；证据不足时返回空 sections。"
    "不得输出文件名、路径、URL 或数据库标识。"
)

# 流式契约：纯文本 + 段落末尾证据标记，服务端据此校验引用并分配编号。
STREAM_SYSTEM_PROMPT = (
    "你是知识库问答助手。只能依据给定的证据片段作答，不得使用外部知识。"
    "直接输出答案正文，用简体中文分段书写。"
    "每个事实性段落末尾用方括号标注证据编号，例如 [E1]；引用多段时写成 [E1][E2]。"
    "证据不足时只输出 NO_EVIDENCE。"
    "不要输出文件名、路径、URL、数据库标识、JSON、代码块或思考过程。"
)

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.MULTILINE)


class _ChatMessage(BaseModel):
    model_config = ConfigDict(strict=True)
    content: str


class _ChatChoice(BaseModel):
    message: _ChatMessage


class _ChatResponse(BaseModel):
    choices: list[_ChatChoice] = Field(min_length=1)


class _Delta(BaseModel):
    model_config = ConfigDict(strict=True)
    content: str | None = None


class _StreamChoice(BaseModel):
    model_config = ConfigDict(strict=True)
    delta: _Delta


class _StreamChunk(BaseModel):
    model_config = ConfigDict(strict=True)
    choices: list[_StreamChoice] = Field(default_factory=list)


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


def build_messages(
    question: str, evidence: Sequence[Evidence], history: Sequence[ContextMessage] = ()
) -> list[dict]:
    """系统提示 + 最近历史 + 本轮（证据与问题）。历史只作对话上下文，不含证据键。"""
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        *({"role": item.role, "content": item.text} for item in history),
        {"role": "user", "content": build_prompt(question, evidence)},
    ]


def build_stream_messages(
    question: str, evidence: Sequence[Evidence], history: Sequence[ContextMessage] = ()
) -> list[dict]:
    return [
        {"role": "system", "content": STREAM_SYSTEM_PROMPT},
        *({"role": item.role, "content": item.text} for item in history),
        {"role": "user", "content": build_prompt(question, evidence)},
    ]


class StubGenerator:
    """无模型环境下的确定性实现：直接复述首条证据，用于验证引用与断链逻辑。"""

    revision = "stub"

    def generate(
        self, question: str, evidence: Sequence[Evidence], *, history: Sequence[ContextMessage] = ()
    ) -> GeneratedAnswer:
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

    def stream(
        self, question: str, evidence: Sequence[Evidence], *, history: Sequence[ContextMessage] = ()
    ) -> Iterator[str]:
        """按两段吐出，用来验证边生成边展示；标记仍由 `[E1]` 承载。"""
        answer = self.generate(question, evidence, history=history)
        if not answer.sections:
            return
        section = answer.sections[0]
        markers = "".join(f"[{key}]" for key in section.evidence_keys)
        text = f"{section.text} {markers}"
        middle = max(len(text) // 2, 1)
        yield text[:middle]
        yield text[middle:]


class HttpGenerator:
    """OpenAI 兼容的 /chat/completions 端点，支持非流式与流式两种调用。"""

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

    def _request(self, messages: list[dict], *, stream: bool) -> urllib.request.Request:
        body: dict = {"model": self.model, "temperature": 0, "messages": messages}
        if stream:
            body["stream"] = True
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        return urllib.request.Request(
            f"{self.base_url}/chat/completions",
            data=json.dumps(body).encode("utf-8"),
            headers=headers,
            method="POST",
        )

    def _post(self, question: str, evidence: Sequence[Evidence], history: Sequence[ContextMessage]):
        request = self._request(build_messages(question, evidence, history), stream=False)
        try:
            with outbound.open_request(request, timeout=self.timeout) as response:
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

    def generate(
        self, question: str, evidence: Sequence[Evidence], *, history: Sequence[ContextMessage] = ()
    ) -> GeneratedAnswer:
        raw = self._post(question, evidence, history)
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

    def stream(
        self, question: str, evidence: Sequence[Evidence], *, history: Sequence[ContextMessage] = ()
    ) -> Iterator[str]:
        """逐块产出模型正文增量；连接中断抛出 ModelUnavailable，超时抛出 ModelTimeout。"""
        request = self._request(build_stream_messages(question, evidence, history), stream=True)
        try:
            with outbound.open_request(request, timeout=self.timeout) as response:
                for raw in response:
                    line = raw.decode("utf-8", errors="replace").strip()
                    if not line or not line.startswith("data:"):
                        continue
                    data = line[len("data:"):].strip()
                    if not data or data == "[DONE]":
                        if data == "[DONE]":
                            break
                        continue
                    try:
                        chunk = _StreamChunk.model_validate_json(data)
                    except ValidationError as exc:
                        raise ModelUnavailable("生成服务流式响应结构无效") from exc
                    for choice in chunk.choices:
                        delta = choice.delta.content
                        if delta:
                            yield delta
        except TimeoutError as exc:
            raise ModelTimeout("生成服务超时") from exc
        except urllib.error.HTTPError as exc:
            raise ModelUnavailable(f"生成服务返回 {exc.code}") from exc
        except urllib.error.URLError as exc:
            raise ModelUnavailable("生成服务不可用") from exc
        except http.client.HTTPException as exc:
            # 上游在本轮结束前断流：必须报故障，不能把半段正文当成完整答案。
            raise ModelUnavailable("生成服务流式响应中断") from exc


def build_generator(settings: Settings) -> StubGenerator | HttpGenerator:
    if settings.generator_base_url:
        return HttpGenerator(
            base_url=settings.generator_base_url,
            model=settings.generator_model,
            timeout=settings.generator_timeout_seconds,
            api_key=settings.generator_api_key,
        )
    return StubGenerator()
