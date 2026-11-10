"""本机重排协议、顺序和故障边界。"""

import io
import json
from unittest.mock import Mock, patch

import pytest

from knowledge.adapters.reranking import HttpReranker
from knowledge.domain.errors import ModelTimeout, ModelUnavailable


@pytest.mark.parametrize(
    "url",
    [
        "https://remote.example/rerank",
        "http://127.0.0.1@remote.example/rerank",
        "http://localhost/rerank?url=x",
    ],
)
def test_reranker_rejects_external_destinations(url):
    with pytest.raises(ModelUnavailable):
        HttpReranker(url, "fixture")


def test_reranker_restores_candidate_order_and_keeps_negative_logits():
    response = io.BytesIO(
        json.dumps(
            {
                "results": [
                    {"index": 1, "relevance_score": 0.2},
                    {"index": 0, "relevance_score": -4.0},
                ]
            }
        ).encode()
    )
    opener = Mock()
    opener.open.return_value = response
    with patch("urllib.request.build_opener", return_value=opener):
        assert HttpReranker("http://127.0.0.1/rerank", "fixture").rank("问题", ["甲", "乙"]) == [
            -4.0,
            0.2,
        ]


@pytest.mark.parametrize(
    "results",
    [
        [{"index": 0, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}],
        [{"index": 0, "relevance_score": float("nan")}, {"index": 1, "relevance_score": 0.2}],
        [{"index": True, "relevance_score": 0.1}, {"index": 0, "relevance_score": 0.2}],
    ],
)
def test_invalid_scores_fail_explicitly(results):
    opener = Mock()
    opener.open.return_value = io.BytesIO(json.dumps({"results": results}).encode())
    with patch("urllib.request.build_opener", return_value=opener), pytest.raises(ModelUnavailable):
        HttpReranker("http://localhost/rerank", "fixture").rank("问题", ["甲", "乙"])


def test_reranker_timeout_is_a_model_failure():
    opener = Mock()
    opener.open.side_effect = TimeoutError
    with patch("urllib.request.build_opener", return_value=opener), pytest.raises(ModelTimeout):
        HttpReranker("http://localhost/rerank", "fixture").rank("问题", ["甲"])
