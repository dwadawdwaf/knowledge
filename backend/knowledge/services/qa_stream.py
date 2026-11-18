"""流式问答编排：阶段事件 + 增量正文 + 引用校验后完成。

实现参照 QwenPaw v1.1.12.post3 的流式任务思路（`app/runner/task_tracker.py`）：
一次运行由独立生产者推进，事件写入队列，订阅方（SSE 响应）逐条消费，结束用哨兵收尾；
订阅方断开时置停止标记，生产者在下一次检查点退出并释放会话与模型连接。
差异是本项目不做事件缓冲回放与重连：问答是一次性的，断开即结束。

同步栈（psycopg、urllib）在独立线程推进，事件通过 `loop.call_soon_threadsafe`
投递到 asyncio 队列，避免跨线程直接操作事件循环对象。
"""

from __future__ import annotations

import asyncio
import json
import threading
import uuid
from collections.abc import AsyncIterator, Callable
from typing import Any

from sqlalchemy.orm import Session

from knowledge.config import Settings
from knowledge.domain.citations import NO_EVIDENCE, MarkerStream
from knowledge.domain.entities import Actor, Answer, AnswerStatus, ContextMessage
from knowledge.domain.errors import DomainError, SourceChanged
from knowledge.domain.ports import Embedder, Generator
from knowledge.logging import get_logger
from knowledge.repositories.session import new_session
from knowledge.services import context as context_service
from knowledge.services import qa, query_rewrite

log = get_logger("qa_stream")

# 事件协议：只描述真实执行阶段，不包含模型思维链。
RUN_STARTED = "run_started"
CONTEXT_STARTED = "context_started"
CONTEXT_COMPLETED = "context_completed"
QUERY_REWRITE_STARTED = "query_rewrite_started"
QUERY_REWRITE_COMPLETED = "query_rewrite_completed"
RETRIEVAL_STARTED = "retrieval_started"
RETRIEVAL_COMPLETED = "retrieval_completed"
GENERATION_STARTED = "generation_started"
ANSWER_DELTA = "answer_delta"
CITATIONS = "citations"
COMPLETED = "completed"
ERROR = "error"

SSE_HEADERS = {
    "Cache-Control": "no-store",
    "X-Accel-Buffering": "no",
}

SessionFactory = Callable[[], Session]


def format_event(event: str, data: dict[str, Any]) -> str:
    """SSE 帧：命名事件 + JSON 载荷。前端按事件名分派，不解析文本格式。"""
    return f"event: {event}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


class _Stopped(Exception):
    """订阅方已断开，生产者主动退出；不是错误，不产生 error 事件。"""


class StreamRun:
    """一次流式问答的生产者/消费者。

    生产者线程拥有数据库会话与模型流，消费者只读事件队列；
    消费者取消后等生产者退出再返回，保证会话与模型连接都已释放。
    """

    def __init__(
        self,
        *,
        actor: Actor,
        settings: Settings,
        embedder: Embedder,
        generator: Generator,
        question: str,
        kb_ids: list[uuid.UUID],
        top_k: int | None = None,
        conversation_id: uuid.UUID | None = None,
        document_ids: list[uuid.UUID] | None = None,
        session_factory: SessionFactory = new_session,
    ) -> None:
        self.run_id = uuid.uuid4()
        self.actor = actor
        self.settings = settings
        self.embedder = embedder
        self.generator = generator
        self.question = question
        self.kb_ids = kb_ids
        self.top_k = top_k
        self.conversation_id = conversation_id
        self.document_ids = document_ids
        self.session_factory = session_factory

        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._produce, name=f"ks-qa-{self.run_id.hex[:8]}", daemon=True
        )
        self._loop: asyncio.AbstractEventLoop | None = None
        self._queue: asyncio.Queue[str | None] | None = None

    # ------------------------------------------------------------ 生产者

    def _emit(self, event: str, data: dict[str, Any]) -> None:
        self._push(format_event(event, data))

    def _push(self, item: str | None) -> None:
        loop, queue = self._loop, self._queue
        if loop is None or queue is None or loop.is_closed():
            return
        try:
            loop.call_soon_threadsafe(queue.put_nowait, item)
        except RuntimeError:  # 事件循环已随断开关闭：剩余事件直接丢弃
            return

    def _checkpoint(self) -> None:
        """阶段与增量之间检查停止标记，让断开能在有限步内生效。"""
        if self._stop.is_set():
            raise _Stopped

    def _produce(self) -> None:
        session = self.session_factory()
        try:
            self._pipeline(session)
        except _Stopped:
            log.info("qa_stream_stopped", extra={"run_id": str(self.run_id)})
        except DomainError as exc:
            log.info("qa_stream_failed", extra={"run_id": str(self.run_id), "code": exc.code})
            self._emit(
                ERROR, {"code": exc.code, "message": exc.message, "retryable": exc.retryable}
            )
        except Exception:  # 流内任何未预期异常都必须转成可见错误
            log.exception("qa_stream_crashed", extra={"run_id": str(self.run_id)})
            self._emit(
                ERROR, {"code": "INTERNAL_ERROR", "message": "服务内部错误", "retryable": False}
            )
        finally:
            session.close()
            self._push(None)

    def _pipeline(self, session: Session) -> None:
        settings = self.settings
        self._emit(
            RUN_STARTED,
            {
                "run_id": str(self.run_id),
                "conversation_id": str(self.conversation_id) if self.conversation_id else None,
            },
        )

        self._checkpoint()
        self._emit(CONTEXT_STARTED, {})
        window = context_service.load_window(
            session, self.actor, settings, self.conversation_id, self.question
        )
        self._emit(
            CONTEXT_COMPLETED,
            {"messages": len(window), "limit": settings.context_recent_messages},
        )

        self._checkpoint()
        self._emit(QUERY_REWRITE_STARTED, {})
        query = query_rewrite.standalone_query(
            window,
            self.question,
            anchor_chars=settings.context_rewrite_max_chars,
            query_chars=settings.query_max_chars,
        )
        self._emit(
            QUERY_REWRITE_COMPLETED, {"query": query.text, "rewritten": query.rewritten}
        )

        self._checkpoint()
        self._emit(RETRIEVAL_STARTED, {})
        found = qa.recall_with_context(
            session,
            self.actor,
            settings,
            self.embedder,
            query=query,
            kb_ids=self.kb_ids,
            top_k=self.top_k,
            document_ids=self.document_ids,
        )
        self._emit(
            RETRIEVAL_COMPLETED,
            {
                "hits": found.hits,
                "degraded": found.degraded,
                "warnings": found.warnings,
                "mode": "hybrid",
            },
        )

        if found.empty:
            self._finish_no_evidence(
                session,
                found,
                window,
                query,
                qa.NO_EVIDENCE_TEXT if found.hits == 0 else qa.INSUFFICIENT_TEXT,
                model_revision="",
            )
            return

        self._checkpoint()
        self._emit(GENERATION_STARTED, {"evidence": len(found.evidence)})
        raw = self._stream_answer(found, window)

        stripped = raw.strip()
        if not stripped or stripped == NO_EVIDENCE:
            # 与空 sections 等价：模型按约定认为证据不足，属正常拒答（C6）。
            self._finish_no_evidence(
                session,
                found,
                window,
                query,
                qa.EMPTY_ANSWER_TEXT,
                model_revision=self.generator.revision,
            )
            return

        body, citations, warnings = qa.assemble_marked(raw, found.evidence)
        warnings = sorted(set(warnings) | set(found.warnings))
        if not qa.sources_unchanged(session, found.evidence_hits):
            log.warning("qa_stream_source_changed", extra={"run_id": str(self.run_id)})
            raise SourceChanged("资料已更新或不可访问，请重新提问")

        self._emit(CITATIONS, {"citations": [item.to_dict() for item in citations]})
        answer = Answer(
            status=AnswerStatus.ANSWERED.value,
            text=body,
            citations=citations,
            warnings=warnings,
            hits=found.hits,
            degraded=found.degraded,
            context_messages=len(window),
            query_rewritten=query.rewritten,
        )
        qa.persist(
            session, self.actor, self.conversation_id, self.question, answer,
            self.generator.revision,
        )
        log.info(
            "qa_stream_answered",
            extra={
                "run_id": str(self.run_id),
                "hits": found.hits,
                "citations": len(citations),
                "context_messages": len(window),
                "query_rewritten": query.rewritten,
            },
        )
        self._emit(COMPLETED, self._completed_payload(answer, found))

    def _stream_answer(self, found: qa.EvidenceSet, window: list[ContextMessage]) -> str:
        """把模型增量转成正文事件，返回完整原文（含标记，供最终校验）。"""
        marker = MarkerStream()
        raw: list[str] = []
        stream = self.generator.stream(self.question, found.evidence, history=window)
        try:
            for delta in stream:
                self._checkpoint()
                raw.append(delta)
                visible = marker.feed(delta)
                if visible:
                    self._emit(ANSWER_DELTA, {"text": visible})
            tail = marker.flush()
            if tail:
                self._emit(ANSWER_DELTA, {"text": tail})
        finally:
            # 正常结束、异常与断开都显式关闭模型流，及时释放上游连接。
            close = getattr(stream, "close", None)
            if close is not None:
                close()
        return "".join(raw)

    def _finish_no_evidence(
        self,
        session: Session,
        found: qa.EvidenceSet,
        window: list[ContextMessage],
        query: query_rewrite.StandaloneQuery,
        text: str,
        *,
        model_revision: str,
    ) -> None:
        log.info("qa_stream_no_evidence", extra={"run_id": str(self.run_id), "hits": found.hits})
        self._checkpoint()
        self._emit(ANSWER_DELTA, {"text": text})
        answer = Answer(
            status=AnswerStatus.NO_EVIDENCE.value,
            text=text,
            hits=found.hits,
            warnings=found.warnings,
            degraded=found.degraded,
            context_messages=len(window),
            query_rewritten=query.rewritten,
        )
        qa.persist(
            session, self.actor, self.conversation_id, self.question, answer, model_revision
        )
        self._emit(COMPLETED, self._completed_payload(answer, found))

    @staticmethod
    def _completed_payload(answer: Answer, found: qa.EvidenceSet) -> dict[str, Any]:
        return {
            "answer_status": answer.status,
            # 校验后的最终正文：前端以它覆盖流式期间显示的临时文本。
            "answer": answer.text,
            "citations": len(answer.citations),
            "hits": found.hits,
            "degraded": found.degraded,
            "warnings": answer.warnings,
            "context_messages": answer.context_messages,
            "query_rewritten": answer.query_rewritten,
        }

    # ------------------------------------------------------------ 消费者

    def request_stop(self) -> None:
        self._stop.set()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        """绑定事件循环并启动生产者；之后用 `events()` 消费。"""
        self._loop = loop
        self._queue = asyncio.Queue()
        self._thread.start()

    async def events(self) -> AsyncIterator[str]:
        """按顺序产出 SSE 帧；消费方取消时置停止标记并等生产者收尾。"""
        assert self._queue is not None
        try:
            while True:
                item = await self._queue.get()
                if item is None:
                    return
                yield item
        finally:
            self.request_stop()
            if self._thread.is_alive():
                await asyncio.to_thread(self._thread.join, 10.0)
