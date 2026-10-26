"""验证完整验收的退出码，不依赖数据库或真实模型。"""

import os
import subprocess
import sys
from pathlib import Path

import pytest


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("def test_ok(): assert True", 0),
        ("import pytest\ndef test_skip(): pytest.skip('missing fixture')", 1),
        ("import pytest\n@pytest.mark.xfail\ndef test_xfail(): assert False", 1),
        ("import pytest\n@pytest.mark.xfail\ndef test_xpass(): assert True", 1),
        ("def test_bad(): assert False", 1),
        ("", 1),
    ],
)
def test_complete_run_exit_code(tmp_path, source, expected):
    case = tmp_path / "test_case.py"
    case.write_text(source, encoding="utf-8")
    env = {key: value for key, value in os.environ.items() if not key.startswith("KS_")}
    env.update(
        PYTHONPATH=str(Path(__file__).resolve().parents[2]),
        PYTEST_ADDOPTS="",
        PYTEST_DISABLE_PLUGIN_AUTOLOAD="1",
        PYTHONIOENCODING="utf-8",
    )
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "-p", "scripts.test_policy", str(case), "-q"],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        encoding="utf-8",
        check=False,
    )
    assert result.returncode == expected, result.stdout + result.stderr


@pytest.mark.parametrize("installed", ["1.0", "2.0", None])
def test_lock_preflight_detects_missing_or_changed_dependency(tmp_path, monkeypatch, installed):
    from importlib.metadata import PackageNotFoundError

    from scripts import check

    backend = tmp_path / "backend"
    backend.mkdir()
    (tmp_path / ".python-version").write_text(
        ".".join(map(str, sys.version_info[:3])), encoding="utf-8"
    )
    (backend / "requirements-dev.lock").write_text("# test\nexample==1.0\n", encoding="utf-8")
    monkeypatch.setattr(check, "BACKEND", backend)

    def fake_version(name):
        if installed is None:
            raise PackageNotFoundError(name)
        return installed

    monkeypatch.setattr(check, "version", fake_version)
    assert check.check_environment() is (installed == "1.0")
