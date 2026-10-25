from unittest.mock import Mock

import pytest

from knowledge.worker import loop


def test_recovery_runs_after_startup_on_every_poll(monkeypatch, settings):
    class StopWorker(Exception):
        pass

    session = Mock()
    recover = Mock(return_value=0)
    process = Mock(side_effect=[False, StopWorker()])
    monkeypatch.setattr(loop, "build_deps", lambda _: Mock(storage=Mock()))
    monkeypatch.setattr(loop, "new_session", lambda: session)
    monkeypatch.setattr(loop.job_repo, "recover_expired", recover)
    monkeypatch.setattr(loop.cleanup, "reclaim_orphans", lambda *args: 0)
    monkeypatch.setattr(loop, "process_next", process)
    monkeypatch.setattr(loop.time, "sleep", lambda _: None)
    with pytest.raises(StopWorker):
        loop.run_forever(settings)
    assert recover.call_count == 3
    assert process.call_count == 2
    assert session.close.call_count == 3
