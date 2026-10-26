"""完整验收禁止跳过或预期失败，避免环境缺失被误报为通过。"""

from __future__ import annotations

import pytest


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("filterwarnings", "error::pytest.PytestUnhandledThreadExceptionWarning")


def pytest_sessionfinish(session: pytest.Session, exitstatus: int) -> None:
    reporter = session.config.pluginmanager.get_plugin("terminalreporter")
    if reporter is None:
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
        return
    incomplete = sum(
        len(reporter.stats.get(kind, [])) for kind in ("skipped", "xfailed", "xpassed", "deselected")
    )
    if incomplete or session.testscollected == 0:
        reporter.write_sep("!", "完整验收失败：存在跳过、预期失败或被排除的用例，或未收集到测试")
        session.exitstatus = pytest.ExitCode.TESTS_FAILED
