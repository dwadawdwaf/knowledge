"""对象存储适配器：本地目录实现。object_key 由服务端生成，不参与用户文件名。"""

from __future__ import annotations

import os
import re
import time
import uuid
from collections.abc import Sequence
from pathlib import Path

from knowledge.domain.errors import NotFound

_SAFE_KEY = re.compile(r"^[A-Za-z0-9._/-]+$")


def new_object_key(media_type: str) -> str:
    suffix = {
        "application/pdf": "pdf",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document": "docx",
        "text/markdown": "md",
        "text/plain": "txt",
        "image/png": "png",
        "image/jpeg": "jpg",
        "image/tiff": "tiff",
    }.get(media_type, "bin")
    return f"{uuid.uuid4().hex[:2]}/{uuid.uuid4().hex}.{suffix}"


class LocalStorage:
    def __init__(self, root: str) -> None:
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)

    def _path(self, object_key: str) -> Path:
        if not _SAFE_KEY.match(object_key) or ".." in object_key:
            raise NotFound("对象不存在")
        path = (self.root / object_key).resolve()
        if not str(path).startswith(str(self.root.resolve())):
            raise NotFound("对象不存在")
        return path

    def put(self, object_key: str, data: bytes) -> None:
        path = self._path(object_key)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        os.replace(tmp, path)

    def get(self, object_key: str) -> bytes:
        path = self._path(object_key)
        if not path.is_file():
            raise NotFound("对象不存在")
        return path.read_bytes()

    def delete(self, object_key: str) -> None:
        path = self._path(object_key)
        if path.is_file():
            path.unlink()

    def exists(self, object_key: str) -> bool:
        return self._path(object_key).is_file()

    def iter_objects(self) -> Sequence[tuple[str, float]]:
        """列出全部对象及已存在的秒数（含写入中断遗留的 .tmp）。"""
        now = time.time()
        objects: list[tuple[str, float]] = []
        for path in sorted(self.root.rglob("*")):
            if path.is_file():
                key = path.relative_to(self.root).as_posix()
                objects.append((key, now - path.stat().st_mtime))
        return objects
