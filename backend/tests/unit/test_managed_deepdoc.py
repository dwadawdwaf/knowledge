"""只复用本项目正在运行的边车配置；不查询其他容器、不改写显式令牌。"""

import json
from unittest.mock import Mock

import pytest

from knowledge.config import Settings
from scripts import real_model


@pytest.mark.parametrize(
    "project,running,accepted",
    [("ks-deepdoc", True, True), ("other", True, False), ("ks-deepdoc", False, False)],
)
def test_attach_requires_project_service_and_running_state(monkeypatch, project, running, accepted):
    token = "synthetic-" * 5
    info = {
        "State": {"Running": running},
        "Config": {
            "Labels": {
                "com.docker.compose.project": project,
                "com.docker.compose.service": "deepdoc",
            },
            "Env": ["DEEPDOC_TOKEN=" + token],
        },
    }
    monkeypatch.setattr(real_model.shutil, "which", lambda _: "docker")
    run = Mock(return_value=Mock(returncode=0, stdout=json.dumps([info])))
    monkeypatch.setattr(real_model.subprocess, "run", run)
    settings = Settings(deepdoc_token=None)
    resolved = real_model.attach_running_deepdoc(settings)
    assert resolved.deepdoc_token == (token if accepted else None)
    assert settings.deepdoc_token is None
    assert run.call_args.args[0][-1] == "ks-deepdoc-deepdoc-1"


def test_explicit_token_and_other_origins_are_not_overridden(monkeypatch):
    run = Mock()
    monkeypatch.setattr(real_model.subprocess, "run", run)
    for settings in [
        Settings(deepdoc_token="explicit"),
        Settings(deepdoc_url="http://127.0.0.1:9999", deepdoc_token=None),
    ]:
        assert real_model.attach_running_deepdoc(settings) is settings
    run.assert_not_called()
