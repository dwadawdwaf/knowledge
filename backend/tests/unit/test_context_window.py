"""Recent Window 与上下文感知检索的纯逻辑用例（不连接数据库）。"""

from __future__ import annotations

from knowledge.domain.entities import ContextMessage
from knowledge.services.context import trim_window
from knowledge.services.query_rewrite import looks_context_dependent, standalone_query


def _messages(count: int) -> list[ContextMessage]:
    out = []
    for index in range(count):
        role = "user" if index % 2 == 0 else "assistant"
        out.append(ContextMessage(role=role, text=f"消息{index}"))
    return out


def test_window_keeps_order_and_applies_limit():
    window = trim_window(_messages(20), "新问题", 12)
    assert len(window) == 12
    assert [item.text for item in window] == [f"消息{index}" for index in range(8, 20)]


def test_window_shorter_than_limit_uses_everything():
    window = trim_window(_messages(3), "新问题", 12)
    assert [item.text for item in window] == ["消息0", "消息1", "消息2"]


def test_current_question_is_not_duplicated_in_window():
    history = [*_messages(4), ContextMessage(role="user", text="它是什么意思？")]
    window = trim_window(history, "  它是什么意思？ ", 12)
    assert [item.role for item in window] == ["user", "assistant", "user", "assistant"]
    assert all(item.text != "它是什么意思？" for item in window)


def test_only_trailing_duplicate_is_dropped():
    """历史中间的同文提问是真实发生过的，不能被一并剔除。"""
    history = [
        ContextMessage(role="user", text="同一句"),
        ContextMessage(role="assistant", text="回答"),
        ContextMessage(role="user", text="别的问题"),
        ContextMessage(role="user", text="同一句"),
    ]
    window = trim_window(history, "同一句", 12)
    assert [item.text for item in window] == ["同一句", "回答", "别的问题"]


def test_zero_limit_disables_context():
    assert trim_window(_messages(6), "问题", 0) == []


def test_blank_and_unknown_roles_are_ignored():
    history = [
        ContextMessage(role="user", text="   "),
        ContextMessage(role="system", text="系统提示"),
        ContextMessage(role="assistant", text="保留"),
    ]
    assert [item.text for item in trim_window(history, "问题", 12)] == ["保留"]


def test_reference_markers_are_detected():
    for question in ["它是什么意思？", "继续解释", "刚才那个方案呢", "这个和 OpenSearch 有什么区别"]:
        assert looks_context_dependent(question) is True
    assert looks_context_dependent("会议室使用后需要做什么？") is False


def test_standalone_query_prepends_previous_question_and_keeps_original():
    history = [ContextMessage(role="user", text="pgvector 是什么？")]
    result = standalone_query(
        history, "它和 OpenSearch 有什么区别？", anchor_chars=400, query_chars=2000
    )
    assert result.rewritten is True
    assert "pgvector 是什么" in result.text
    assert result.text.endswith("它和 OpenSearch 有什么区别？")
    assert result.original == "它和 OpenSearch 有什么区别？"


def test_standalone_query_passes_through_without_history_or_markers():
    history = [ContextMessage(role="user", text="pgvector 是什么？")]
    plain = standalone_query(history, "OpenSearch 支持哪些查询？", anchor_chars=400, query_chars=2000)
    assert plain.rewritten is False and plain.text == "OpenSearch 支持哪些查询？"

    no_history = standalone_query([], "它是什么？", anchor_chars=400, query_chars=2000)
    assert no_history.rewritten is False and no_history.text == "它是什么？"


def test_standalone_query_respects_budget_and_keeps_question_intact():
    history = [ContextMessage(role="assistant", text="很长的上文" * 100)]
    result = standalone_query(history, "继续", anchor_chars=50, query_chars=60)
    assert result.rewritten is True
    assert len(result.text) <= 60
    assert result.text.endswith("继续")

    disabled = standalone_query(history, "继续", anchor_chars=0, query_chars=60)
    assert disabled.rewritten is False and disabled.text == "继续"
