"""结构化日志格式：extra 字段并入 JSON，正文与令牌类字段不特殊处理（由调用方保证不传）。"""

from __future__ import annotations

import json
import logging

from knowledge.logging import JsonFormatter, get_logger, setup_logging


def test_formatter_serializes_extra_fields() -> None:
    formatter = JsonFormatter()
    record = logging.LogRecord(
        "knowledge.worker", logging.INFO, __file__, 1, "job_claimed", None, None
    )
    record.job_id = "0192a"
    record.build_id = "0192b"
    record.queue_seconds = 1.5

    payload = json.loads(formatter.format(record))

    assert payload["event"] == "job_claimed"
    assert payload["job_id"] == "0192a"
    assert payload["build_id"] == "0192b"
    assert payload["queue_seconds"] == 1.5
    assert payload["level"] == "INFO"
    assert "ts" in payload
    # LogRecord 内部属性不泄漏进载荷
    assert "args" not in payload
    assert "lineno" not in payload


def test_setup_logging_wires_json_handler() -> None:
    setup_logging()
    root = logging.getLogger("knowledge")
    try:
        assert any(isinstance(handler.formatter, JsonFormatter) for handler in root.handlers)
        assert get_logger("worker").name == "knowledge.worker"
    finally:
        root.handlers.clear()
        root.propagate = True
