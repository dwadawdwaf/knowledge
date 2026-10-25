"""Killable parsing subprocess with wall-time, CPU, RSS and output limits."""

from __future__ import annotations

import json
import multiprocessing
import time
from dataclasses import asdict
from multiprocessing.connection import Connection

import psutil

from knowledge.config import Settings
from knowledge.domain.entities import Locator, ParsedBlock
from knowledge.domain.errors import ParseFailed, PasswordProtected
from knowledge.domain.ports import Parser


def _parse_child(
    connection: Connection,
    parser: Parser,
    data: bytes,
    media_type: str,
    filename: str,
    output_limit: int,
) -> None:
    try:
        blocks = parser.parse(data, media_type, filename)
        payload = json.dumps({"blocks": [asdict(block) for block in blocks]}).encode()
        if len(payload) > output_limit:
            raise ParseFailed("解析输出超限")
    except PasswordProtected:
        payload = b'{"error":"PASSWORD_PROTECTED"}'
    except Exception:  # noqa: BLE001 - Never serialize parser exceptions across the boundary.
        payload = b'{"error":"PARSE_FAILED"}'
    try:
        connection.send_bytes(payload)
    finally:
        connection.close()


class BoundedParser:
    def __init__(self, parser: Parser, settings: Settings) -> None:
        self.parser = parser
        self.name = parser.name
        self.timeout = settings.parse_timeout_seconds
        self.cpu_limit = settings.parse_cpu_seconds
        self.memory_limit = settings.parse_memory_bytes
        self.output_limit = settings.parse_output_bytes

    def supports(self, media_type: str, filename: str) -> bool:
        return self.parser.supports(media_type, filename)

    def parse(self, data: bytes, media_type: str, filename: str) -> list[ParsedBlock]:
        context = multiprocessing.get_context("spawn")
        reader, writer = context.Pipe(duplex=False)
        process = context.Process(
            target=_parse_child,
            args=(writer, self.parser, data, media_type, filename, self.output_limit),
        )
        started = time.monotonic()
        try:
            process.start()
            writer.close()
            assert process.pid is not None
            monitor = psutil.Process(process.pid)
            while True:
                if time.monotonic() - started > self.timeout:
                    raise ParseFailed("解析时间超限")
                try:
                    cpu = monitor.cpu_times()
                    if (
                        monitor.memory_info().rss > self.memory_limit
                        or cpu.user + cpu.system > self.cpu_limit
                    ):
                        raise ParseFailed("解析资源超限")
                except psutil.NoSuchProcess:
                    pass
                if reader.poll(0.02):
                    result = json.loads(reader.recv_bytes(maxlength=self.output_limit))
                    if result.get("error") == "PASSWORD_PROTECTED":
                        raise PasswordProtected("文件已加密")
                    if result.get("error"):
                        raise ParseFailed("文件解析失败")
                    return [
                        ParsedBlock(text=block["text"], locator=Locator(**block["locator"]))
                        for block in result["blocks"]
                    ]
                if not process.is_alive():
                    raise ParseFailed("解析进程退出")
        except (EOFError, OSError, ValueError) as exc:
            raise ParseFailed("文件解析失败") from exc
        finally:
            reader.close()
            writer.close()
            if process.pid is not None:
                if process.is_alive():
                    process.terminate()
                process.join(timeout=2)
                if process.is_alive():
                    process.kill()
                    process.join()
                process.close()
