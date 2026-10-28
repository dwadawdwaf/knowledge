"""真实 HTTP 与独立 Worker 夹具，由隔离测试入口显式启用。"""

from __future__ import annotations

import os

import pytest

from scripts.local_runtime import LocalRuntime
from scripts.model_fixture import ModelFixture


@pytest.fixture(scope="module")
def runtime():
    if os.getenv("KS_RUN_E2E") != "1":
        pytest.skip("Run scripts/test_isolated.py to enable process-level E2E")
    with (
        ModelFixture() as model,
        LocalRuntime(
            database_url=os.getenv("KS_E2E_DATABASE_URL"),
            embedding_url=model.url,
            generator_url=model.url,
            # 隔离入口创建的 e2e_test 是本机临时库，不是 runbook §17 的外部库；
            # 外部库的 60/15 秒租约会让崩溃恢复用例等满 60 秒才回收，40 秒等待必然超时。
            lease_seconds=8,
            heartbeat_seconds=5,
        ) as active,
    ):
        active.model_fixture = model
        yield active


@pytest.fixture(autouse=True)
def reset_runtime(runtime):
    runtime.model_fixture.reset()
    runtime.start_worker()
    yield
    runtime.model_fixture.reset()
    runtime.start_worker()
