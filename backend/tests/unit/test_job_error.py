import json
import logging

import pytest

from knowledge.domain.errors import public_job_error
from knowledge.logging import JsonFormatter


@pytest.mark.parametrize("code", ["PARSE_FAILED", "INTERNAL_ERROR", "secret-code"])
def test_job_error_never_exposes_original_message(code):
    error = public_job_error(
        {"code": code, "message": "SELECT secret /private/file", "retryable": True}
    )
    assert error is not None
    assert "secret" not in json.dumps(error)
    assert "SELECT" not in json.dumps(error)
    assert error["retryable"] is True


def test_exception_logging_does_not_include_exception_payload():
    exc = RuntimeError("SELECT secret /private/file")
    record = logging.LogRecord(
        "knowledge.worker", logging.ERROR, __file__, 1, "task_failed", (), (type(exc), exc, None)
    )
    output = JsonFormatter().format(record)
    assert "secret" not in output
    assert "RuntimeError" in output
