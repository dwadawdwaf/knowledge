"""仅监听本机的确定性模型接口桩，不是真实语言模型。"""

from __future__ import annotations

import json
import threading
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
                    if mode == "503":
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
