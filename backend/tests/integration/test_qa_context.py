"""多轮会话：历史进入模型、Recent Window 上限、跨会话隔离与流式事件顺序。"""

from __future__ import annotations

import asyncio
import json
import os

import pytest
from sqlalchemy.orm import Session

from knowledge.domain.entities import (
    Actor,
    ContextMessage,
    GeneratedAnswer,
    GeneratedSection,
)
from knowledge.domain.errors import NotFound
from knowledge.repositories import conversations as conversation_repo
from knowledge.repositories.tables import Message
from knowledge.services import conversations as conversation_service
from knowledge.services import document as document_service
from knowledge.services import knowledge as knowledge_service
from knowledge.services import qa as qa_service
from knowledge.services import qa_stream, retrieval
from knowledge.worker.loop import process_next
from samples import MEETING_RULES

DATABASE_URL = os.getenv("KS_TEST_DATABASE_URL")

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not DATABASE_URL, reason="未设置 KS_TEST_DATABASE_URL"),
]


class RecordingGenerator:
    """记录每轮实际收到的历史，并按证据复述答案；用于验证上下文是否真的进了模型。"""

    revision = "recording"

    def __init__(self) -> None:
        self.calls: list[dict] = []

    def _record(self, question, evidence, history) -> None:
        self.calls.append(
            {
                "question": question,
                "history": list(history),
                "evidence": list(evidence),
            }
        )

    def generate(self, question, evidence, *, history=()) -> GeneratedAnswer:
        self._record(question, evidence, history)
        if not evidence:
            return GeneratedAnswer(sections=())
        first = evidence[0]
        return GeneratedAnswer(
            sections=(
                GeneratedSection(text=f"依据：{first.text[:60]}", evidence_keys=(first.key,)),
            ),
            model_revision=self.revision,
        )

    def stream(self, question, evidence, *, history=()):
        self._record(question, evidence, history)
        if not evidence:
            return
        text = f"依据：{evidence[0].text[:60]} [E1]"
        middle = len(text) // 2
        yield text[:middle]
        yield text[middle:]


class UncitedGenerator(RecordingGenerator):
    """故意不输出证据标记的模型，用来验证流式路径不会绕开引用校验。"""

    def stream(self, question, evidence, *, history=()):
        self._record(question, evidence, history)
        yield "没有任何标记的答案"


def _seed(session, settings, deps, owner: str = "ctx-user", name: str = "上下文库"):
    actor = Actor(subject_id=owner, tenant_id="default")
    kb = knowledge_service.create(session, actor, name)
    document_service.upload(
        session,
        actor,
        settings,
        deps.storage,
        deps.registry,
        kb_id=kb.id,
        filename="meeting-room.md",
        data=MEETING_RULES,
        embedder_model=deps.embedder.model,
        embedder_dim=deps.embedder.dimension,
    )
    assert process_next(session, deps) is True
    return actor, kb


def _ask(session, settings, deps, actor, generator, kb, conversation_id, question):
    return qa_service.answer(
        session,
        actor,
        settings,
        deps.embedder,
        generator,
        question=question,
        kb_ids=[kb.id],
        conversation_id=conversation_id,
    )


def test_history_enters_llm_context_on_next_turn(session, settings, deps):
    actor, kb = _seed(session, settings, deps)
    conversation = conversation_service.create(session, actor, "多轮")
    generator = RecordingGenerator()

    first = _ask(
        session, settings, deps, actor, generator, kb, conversation.id, "会议室使用后需要做什么"
    )
    assert first.status == "answered"
    assert generator.calls[0]["history"] == []

    second = _ask(
        session, settings, deps, actor, generator, kb, conversation.id, "它还有别的要求吗"
    )
    assert second.status == "answered"
    history = generator.calls[1]["history"]
    assert [item.role for item in history] == ["user", "assistant"]
    assert history[0].text == "会议室使用后需要做什么"
    assert history[1].text == first.text
    assert second.context_messages == 2


def test_recent_window_is_limited_to_twelve_messages(session, settings, deps):
    actor, kb = _seed(session, settings, deps, owner="ctx-window")
    conversation = conversation_service.create(session, actor, "长会话")
    # 每轮一次提交：真实路径下每轮问答是独立事务，created_at 据此可区分先后。
    for index in range(10):
        conversation_repo.append(
            session,
            conversation.id,
            question=f"历史问题{index}",
            answer=f"历史回答{index}",
            answer_status="answered",
            citations=[],
            model_revision="stub",
        )
        session.commit()

    generator = RecordingGenerator()
    answer = _ask(session, settings, deps, actor, generator, kb, conversation.id, "会议室怎么管理")
    history = generator.calls[0]["history"]
    assert len(history) == settings.context_recent_messages == 12
    assert answer.context_messages == 12
    # 保留最近 6 轮，顺序仍是发生顺序
    assert history[0].text == "历史问题4"
    assert history[-1].text == "历史回答9"
    assert [item.role for item in history[:2]] == ["user", "assistant"]


def test_current_question_already_stored_is_not_repeated(session, settings, deps):
    actor, kb = _seed(session, settings, deps, owner="ctx-dedupe")
    conversation = conversation_service.create(session, actor, "重复提问")
    question = "会议室使用后需要做什么"
    conversation_repo.append(
        session,
        conversation.id,
        question=question,
        answer="上一轮回答",
        answer_status="answered",
        citations=[],
        model_revision="stub",
    )
    session.commit()
    # 模拟本轮问题已先落库：窗口尾部必须是这条 user，重复时整条丢弃。
    session.add(Message(conversation_id=conversation.id, role="user", body=question))
    session.commit()

    generator = RecordingGenerator()
    _ask(session, settings, deps, actor, generator, kb, conversation.id, question)
    history = generator.calls[0]["history"]
    assert [item.role for item in history] == ["user", "assistant"]
    assert [item.text for item in history] == [question, "上一轮回答"]


def test_conversations_do_not_share_history(session, settings, deps):
    actor, kb = _seed(session, settings, deps, owner="ctx-isolation")
    left = conversation_service.create(session, actor, "左")
    right = conversation_service.create(session, actor, "右")
    generator = RecordingGenerator()

    _ask(session, settings, deps, actor, generator, kb, left.id, "会议室使用后需要做什么")
    _ask(session, settings, deps, actor, generator, kb, right.id, "借用会议室需要提前登记吗")
    _ask(session, settings, deps, actor, generator, kb, right.id, "它需要谁来安排")

    assert generator.calls[1]["history"] == []
    right_history = [item.text for item in generator.calls[2]["history"]]
    assert right_history[0] == "借用会议室需要提前登记吗"
    assert "会议室使用后需要做什么" not in " ".join(right_history)


def test_other_owner_cannot_use_someone_elses_conversation(session, settings, deps):
    actor, kb = _seed(session, settings, deps, owner="ctx-owner")
    other = Actor(subject_id="ctx-intruder", tenant_id="default")
    conversation = conversation_service.create(session, actor, "私有会话")
    _ask(session, settings, deps, actor, RecordingGenerator(), kb, conversation.id, "第一问")

    with pytest.raises(NotFound):
        conversation_service.detail(session, other, conversation.id)
    with pytest.raises(NotFound):
        qa_service.answer(
            session,
            other,
            settings,
            deps.embedder,
            RecordingGenerator(),
            question="它是什么",
            kb_ids=[kb.id],
            conversation_id=conversation.id,
        )


def test_pronoun_question_rewrites_retrieval_query(session, settings, deps, monkeypatch):
    actor, kb = _seed(session, settings, deps, owner="ctx-rewrite")
    conversation = conversation_service.create(session, actor, "指代")
    generator = RecordingGenerator()
    _ask(session, settings, deps, actor, generator, kb, conversation.id, "会议室使用后需要做什么")

    seen: list[str] = []
    original = retrieval.search

    def spy(*args, **kwargs):
        seen.append(kwargs["query"])
        return original(*args, **kwargs)

    monkeypatch.setattr(retrieval, "search", spy)
    answer = _ask(
        session, settings, deps, actor, generator, kb, conversation.id, "它还有什么要求"
    )

    assert answer.query_rewritten is True
    assert "会议室使用后需要做什么" in seen[0]
    assert seen[0].endswith("它还有什么要求")
    # 模型仍拿到用户原话，历史单独作为消息
    assert generator.calls[-1]["question"] == "它还有什么要求"


def _session_factory(session: Session):
    engine = session.get_bind()
    return lambda: Session(engine, expire_on_commit=False)


def _consume(run: qa_stream.StreamRun) -> list[tuple[str, dict]]:
    async def collect() -> list[tuple[str, dict]]:
        run.start(asyncio.get_running_loop())
        frames = [frame async for frame in run.events()]
        parsed = []
        for frame in frames:
            lines = frame.rstrip("\n").split("\n")
            parsed.append((lines[0].removeprefix("event: "), json.loads(lines[1].removeprefix("data: "))))
        return parsed

    return asyncio.run(collect())


def test_streaming_emits_stage_order_and_validated_citations(session, settings, deps):
    actor, kb = _seed(session, settings, deps, owner="ctx-stream")
    conversation = conversation_service.create(session, actor, "流式")
    _ask(session, settings, deps, actor, RecordingGenerator(), kb, conversation.id, "会议室使用后需要做什么")

    run = qa_stream.StreamRun(
        actor=actor,
        settings=settings,
        embedder=deps.embedder,
        generator=RecordingGenerator(),
        question="它还有什么要求",
        kb_ids=[kb.id],
        conversation_id=conversation.id,
        session_factory=_session_factory(session),
    )
    events = _consume(run)
    names = [name for name, _ in events]

    expected = [
        "run_started",
        "context_started",
        "context_completed",
        "query_rewrite_started",
        "query_rewrite_completed",
        "retrieval_started",
        "retrieval_completed",
        "generation_started",
        "answer_delta",
        "answer_delta",
        "citations",
        "completed",
    ]
    assert names == expected

    payload = dict(events)
    assert payload["context_completed"]["messages"] == 2
    assert payload["query_rewrite_completed"]["rewritten"] is True
    assert payload["citations"]["citations"][0]["index"] == 1
    assert payload["completed"]["answer_status"] == "answered"
    assert payload["completed"]["context_messages"] == 2
    assert "[E1]" not in payload["completed"]["answer"]
    # 增量拼起来就是最终正文（标记已在服务端移除）
    deltas = "".join(data["text"] for name, data in events if name == "answer_delta")
    assert deltas.strip() == payload["completed"]["answer"].strip()

    _stored, messages = conversation_service.detail(session, actor, conversation.id)
    assert [item.role for item in messages] == ["user", "assistant", "user", "assistant"]
    assert messages[-1].body == payload["completed"]["answer"]
    assert messages[-1].citations


def test_streaming_answer_without_citations_reports_error_and_saves_nothing(
    session, settings, deps
):
    actor, kb = _seed(session, settings, deps, owner="ctx-stream-bad")
    conversation = conversation_service.create(session, actor, "无引用")

    run = qa_stream.StreamRun(
        actor=actor,
        settings=settings,
        embedder=deps.embedder,
        generator=UncitedGenerator(),
        question="会议室使用后需要做什么",
        kb_ids=[kb.id],
        conversation_id=conversation.id,
        session_factory=_session_factory(session),
    )
    events = _consume(run)
    names = [name for name, _ in events]
    assert "completed" not in names
    assert names[-1] == "error"
    assert dict(events)["error"]["code"] == "CITATION_VALIDATION_FAILED"

    _conversation, messages = conversation_service.detail(session, actor, conversation.id)
    assert messages == []


def test_streaming_no_evidence_completes_without_generation(session, settings, deps):
    actor, kb = _seed(session, settings, deps, owner="ctx-stream-none")
    conversation = conversation_service.create(session, actor, "无证据")

    run = qa_stream.StreamRun(
        actor=actor,
        settings=settings,
        embedder=deps.embedder,
        generator=RecordingGenerator(),
        question="完全无关的问题 xyzzy",
        kb_ids=[kb.id],
        conversation_id=conversation.id,
        session_factory=_session_factory(session),
    )
    events = _consume(run)
    names = [name for name, _ in events]
    assert "generation_started" not in names
    assert names[-1] == "completed"
    assert dict(events)["completed"]["answer_status"] == "no_evidence"


def test_context_messages_are_plain_roles_for_the_model(session, settings, deps):
    """窗口内容只能是 user/assistant，且不含服务端标识。"""
    actor, kb = _seed(session, settings, deps, owner="ctx-shape")
    conversation = conversation_service.create(session, actor, "形状")
    conversation_repo.append(
        session,
        conversation.id,
        question="历史提问",
        answer="历史回答[1]",
        answer_status="answered",
        citations=[{"index": 1, "chunk_id": "00000000-0000-0000-0000-000000000000"}],
        model_revision="stub",
    )
    session.commit()

    generator = RecordingGenerator()
    _ask(session, settings, deps, actor, generator, kb, conversation.id, "会议室怎么管理")
    history = generator.calls[0]["history"]
    assert all(isinstance(item, ContextMessage) for item in history)
    assert {item.role for item in history} <= {"user", "assistant"}
    assert history[1].text == "历史回答[1]"
