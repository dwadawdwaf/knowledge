"""流式事件帧格式（不连接数据库）。"""

from __future__ import annotations

import json

from knowledge.services.qa_stream import format_event


def test_event_frame_is_named_and_json_encoded():
    frame = format_event("answer_delta", {"text": "中文"})
    assert frame.startswith("event: answer_delta\ndata: ")
    assert frame.endswith("\n\n")


def test_event_data_round_trips_without_ascii_escaping():
    frame = format_event("completed", {"answer": "结论[1]", "warnings": []})
    payload = frame.split("data: ", 1)[1].strip()
    assert "结论" in payload
    assert json.loads(payload) == {"answer": "结论[1]", "warnings": []}
