from __future__ import annotations

import time

import pytest

from knowledge.domain.errors import AuthenticationFailed
from knowledge.security import issue_token, verify_token


def test_roundtrip(settings):
    token = issue_token("user-a", "default", 600, settings)
    actor = verify_token(token, settings)
    assert actor.subject_id == "user-a"
    assert actor.tenant_id == "default"


def test_tampered_payload_rejected(settings):
    token = issue_token("user-a", "default", 600, settings)
    payload, signature = token.split(".", 1)
    forged = payload[:-2] + ("AA" if not payload.endswith("AA") else "BB") + "." + signature
    with pytest.raises(AuthenticationFailed):
        verify_token(forged, settings)


def test_expired_rejected(settings):
    token = issue_token("user-a", "default", -1, settings)
    time.sleep(0.01)
    with pytest.raises(AuthenticationFailed):
        verify_token(token, settings)


def test_wrong_secret_rejected(settings):
    token = issue_token("user-a", "default", 600, settings)
    other = settings.model_copy(update={"token_secret": "another-secret"})
    with pytest.raises(AuthenticationFailed):
        verify_token(token, other)


def test_malformed_rejected(settings):
    with pytest.raises(AuthenticationFailed):
        verify_token("not-a-token", settings)
