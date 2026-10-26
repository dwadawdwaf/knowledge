"""重建只接收版本与代次，禁止身份或配置注入。"""

import uuid

import pytest
from pydantic import ValidationError

from knowledge.api.v1.schemas.requests import ReindexRequest


@pytest.mark.parametrize(
    "change",
    [
        {"expected_generation": -1},
        {"expected_generation": True},
        {"expected_generation": "1"},
        {"owner_id": "other"},
        {"tenant_id": "other"},
        {"embedding_model": "different"},
    ],
)
def test_reindex_rejects_invalid_or_extra_fields(change):
    body = {"version_id": str(uuid.uuid4()), "expected_generation": 1, **change}
    with pytest.raises(ValidationError):
        ReindexRequest.model_validate(body)


def test_reindex_requires_explicit_generation():
    with pytest.raises(ValidationError):
        ReindexRequest.model_validate({"version_id": str(uuid.uuid4())})
