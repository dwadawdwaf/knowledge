"""仅监听本机的确定性模型接口桩，不是真实语言模型。"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Self

from knowledge.adapters.embedding import HashEmbedder


class ModelFixture:
    def __init__(self) -> None:
        self.embedding_entered = threading.Event()
        self._embedding_gate = threading.Event()
        self._embedding_gate.set()
        self.generator_failure: str | None = None
        self.embedding_failure = False
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.url = f"http://127.0.0.1:{self.server.server_port}"

    def _handler(self) -> type[BaseHTTPRequestHandler]:
        fixture = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, format: str, *args: object) -> None:
                pass

            def do_POST(self) -> None:
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/embeddings":
                    fixture.embedding_entered.set()
                    if fixture.embedding_failure:
                        self._reply(503, {"error": "synthetic embedding outage"})
                        return
                    if not fixture._embedding_gate.wait(30):
                        self._reply(503, {"error": "fixture timeout"})
                        return
                    vectors = HashEmbedder().embed(payload["input"])
                    self._reply(
                        200,
                        {
                            "data": [
                                {"index": index, "embedding": vector}
                                for index, vector in enumerate(vectors)
                            ]
                        },
                    )
                elif self.path == "/chat/completions":
                    mode = fixture.generator_failure
                    if payload.get("stream"):
                        self._stream(payload, mode)
                    elif mode == "503":
                        self._reply(503, {"error": "synthetic outage"})
                    elif mode == "invalid-envelope":
                        self._reply(200, [])
                    else:
                        prompt = payload["messages"][-1]["content"]
                        evidence = prompt.split("[E1] ", 1)[1].split("\n[E2]", 1)[0]
                        evidence = evidence.split("\n\n问题：", 1)[0].strip()
                        content = (
                            []
                            if mode == "invalid-sections"
                            else {"sections": [{"text": evidence, "evidence_keys": ["E1"]}]}
                        )
                        self._reply(
                            200, {"choices": [{"message": {"content": json.dumps(content)}}]}
                        )
                else:
                    self._reply(404, {"error": "unknown fixture path"})

            def _stream(self, payload: dict, mode: str | None) -> None:
                """流式契约：纯文本 + [E#] 标记，按小块推送；可注入中途异常。"""
                if mode == "503":
                    self._reply(503, {"error": "synthetic outage"})
                    return
                prompt = payload["messages"][-1]["content"]
                evidence = prompt.split("[E1] ", 1)[1].split("\n[E2]", 1)[0]
                evidence = evidence.split("\n\n问题：", 1)[0].strip()
                text = (
                    "依据资料：" if mode == "unmarked" else f"{evidence} [E1]"
                )
                chunks = [text[index:index + 8] for index in range(0, len(text), 8)]
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.end_headers()
                try:
                    for index, piece in enumerate(chunks):
                        if mode == "stream-error" and index == 1:
                            # 半条 JSON：调用方必须把它当作模型故障，不能当成正常结束。
                            self.wfile.write(b"data: {\"choices\": [\n\n")
                            self.wfile.flush()
                            self.close_connection = True
                            return
                        frame = {"choices": [{"delta": {"content": piece}}]}
                        self.wfile.write(f"data: {json.dumps(frame)}\n\n".encode())
                        self.wfile.flush()
                        if mode == "stream-slow":
                            time.sleep(0.2)
                    self.wfile.write(b"data: [DONE]\n\n")
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    # 客户端中断：桩不需要清理，交由被测服务自行处理。
                    pass

            def _reply(self, status: int, payload: object) -> None:
                data = json.dumps(payload).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                try:
                    self.wfile.write(data)
                except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
                    pass

        return Handler

    def block_embeddings(self) -> None:
        self.embedding_entered.clear()
        self._embedding_gate.clear()

    def release_embeddings(self) -> None:
        self._embedding_gate.set()

    def reset(self) -> None:
        self.generator_failure = None
        self.embedding_failure = False
        self.release_embeddings()

    def __enter__(self) -> Self:
        self.thread.start()
        return self

    def __exit__(self, *args: object) -> None:
        self.release_embeddings()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
