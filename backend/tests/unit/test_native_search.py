"""原生索引失败处理、过滤条件与候选窗口契约。"""
import uuid

import pytest

from knowledge.adapters import ragflow_opensearch
from knowledge.adapters.ragflow_opensearch import NativeIndex
from knowledge.domain.errors import KeywordIndexUnavailable


def test_native_keyword_scopes_before_topk_and_uses_bm25(settings, monkeypatch):
    index = NativeIndex(settings)
    build, key = uuid.uuid4(), uuid.uuid4()
    query = {"query_string": {"query": '"pressure valve"', "fields": ["title_tks^10", "content_ltks^2"]}}
    monkeypatch.setattr(ragflow_opensearch, "native_query", lambda *_: query)
    recorded = []

    def request(method, path, body=None, **kwargs):
        recorded.append(body)
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_id": str(key), "_score": 2.3}]}}

    monkeypatch.setattr(index, "request", request)
    assert index.search("synthetic", [build], "keyword", None, 20) == [(key, 2.3)]
    assert recorded[0]["query"]["bool"]["must"] == [query]
    assert recorded[0]["query"]["bool"]["filter"] == [{"terms": {"build_id": [str(build)]}}]
    assert recorded[0]["size"] == 20


def test_native_knn_has_the_same_text_and_scope_filter(settings, monkeypatch):
    index = NativeIndex(settings)
    build = uuid.uuid4()
    monkeypatch.setattr(ragflow_opensearch, "native_query", lambda *_: {"match_all": {}})
    recorded = []

    def request(method, path, body=None, **kwargs):
        recorded.append(body)
        return {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": []}}

    monkeypatch.setattr(index, "request", request)
    index.search("synthetic", [build], "hybrid", [1.0] * 1024, 80)
    lanes = recorded[0]["query"]["bool"]["should"]
    lexical = lanes[0]["bool"]["must"][0]
    dense = lanes[1]["bool"]["must"][0]["function_score"]
    assert dense["query"]["knn"]["embedding"]["filter"] == lexical
    assert dense["min_score"] == .6
    assert [lane["bool"]["boost"] for lane in lanes] == [.05, 1.0]


@pytest.mark.parametrize("response", [
    {"timed_out": True}, {"timed_out": False, "_shards": {"failed": 1}},
    {"timed_out": False, "_shards": {"failed": 0}, "hits": {"hits": [{"_id": "invalid", "_score": 1}]}},
])
def test_native_partial_or_invalid_results_are_not_empty_answers(settings, monkeypatch, response):
    index = NativeIndex(settings)
    monkeypatch.setattr(index, "request", lambda *_args, **_kwargs: response)
    with pytest.raises(KeywordIndexUnavailable):
        index.search("synthetic", [uuid.uuid4()], "semantic", [1.0] * 1024, 20)


def test_partial_bulk_never_publishes_a_build_marker(settings, monkeypatch):
    index = NativeIndex(settings)
    monkeypatch.setattr(index, "create", lambda: None)
    monkeypatch.setattr(ragflow_opensearch, "token_fields", lambda *_: {})
    sent = []

    def request(method, path, body=None, **kwargs):
        sent.append(body)
        return {"errors": True}

    monkeypatch.setattr(index, "request", request)
    build = uuid.uuid4()
    with pytest.raises(KeywordIndexUnavailable):
        index.index([build], [{"id": uuid.uuid4(), "build_id": build, "embedding": [1.0] * 1024,
                               "text": "synthetic", "title": "test", "structure": {}}])
    assert len(sent) == 1 and b'build-' not in sent[0]


def test_failed_marker_refresh_cannot_report_index_success(settings, monkeypatch):
    index = NativeIndex(settings)
    monkeypatch.setattr(index, "create", lambda: None)
    monkeypatch.setattr(ragflow_opensearch, "token_fields", lambda *_: {})
    refreshed = []

    def request(method, path, body=None, **kwargs):
        if path == "/_bulk":
            return {"errors": False}
        assert path == "/_refresh"
        refreshed.append(True)
        return {"_shards": {"failed": 0 if len(refreshed) == 1 else 1}}

    monkeypatch.setattr(index, "request", request)
    build = uuid.uuid4()
    with pytest.raises(KeywordIndexUnavailable, match="完成标记刷新失败"):
        index.index([build], [{"id": uuid.uuid4(), "build_id": build, "embedding": [1.0] * 1024,
                               "text": "synthetic", "title": "test", "structure": {}}])
