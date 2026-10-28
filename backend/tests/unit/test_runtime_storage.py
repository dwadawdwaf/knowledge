"""对象卷的落点规则：持久库必须显式配持久卷，否则重开一次原文就全部 404。"""

from __future__ import annotations

import pathlib

from scripts.local_runtime import LocalRuntime


def _runtime(tmp_path: pathlib.Path, **kwargs) -> LocalRuntime:
    runtime = LocalRuntime(database_url="postgresql+psycopg://user:pass@127.0.0.1:5432/db", **kwargs)
    runtime.directory = tmp_path / "run"
    runtime.directory.mkdir(exist_ok=True)
    return runtime


def test_default_uses_temporary_volume(tmp_path: pathlib.Path) -> None:
    """没显式指定时用临时卷：不同库共用一个卷会让孤儿回收互删对象。"""
    runtime = _runtime(tmp_path)
    for external in (True, False):
        expected = (runtime.directory / "objects").resolve()
        assert runtime._objects_root(external_database=external) == expected


def test_explicit_root_is_stable_across_runs(tmp_path: pathlib.Path) -> None:
    """同一个持久库重复启动时，对象目录必须指向同一个固定位置。"""
    objects = tmp_path / "shared-objects"
    first = _runtime(tmp_path, storage_root=str(objects))
    second = _runtime(tmp_path, storage_root=str(objects))
    assert first._objects_root(external_database=True) == objects.resolve()
    assert second._objects_root(external_database=True) == objects.resolve()
    assert objects.is_dir()
