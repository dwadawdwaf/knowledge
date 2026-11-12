"""准备 RAGFlow 的固定词库，运行服务时只读取本地资源。"""

from __future__ import annotations

import argparse
import hashlib
import shutil
import tempfile
import urllib.request
from pathlib import Path
from zipfile import ZipFile

_CL100K = (
    "https://openaipublic.blob.core.windows.net/encodings/cl100k_base.tiktoken",
    1_681_126,
    "223921b76ee99bde995b7ff738513eef100fb51d18c93597a113bcffe865b2a7",
)
_WORDNET = (
    "https://raw.githubusercontent.com/nltk/nltk_data/gh-pages/packages/corpora/wordnet.zip",
    10_775_600,
    "cbda5ea6eef7f36a97a43d4a75f85e07fccbb4f23657d27b4ccbc93e2646ab59",
)
_INFINITY_SDK = (
    ("https://files.pythonhosted.org/packages/7c/9d/8322c9a3e13a721dedb37c5e978221b263cfbc3dae0644c5ad283c782235/"
     "infinity_sdk-0.7.0.dev2-py3-none-any.whl"),
    29_818_810,
    "8d7a071ad25a24b50e779f77d1997b792455349e87d259abcafb00539b24cd35",
)
_HUQIE_BYTES = 8_306_137
_HUQIE_SHA256 = "c0b19769ba86db1fd5d6aa14db7ffbe2cafcc39ac0b03003f0add8bc929bab0a"


def _valid(path: Path, size: int, digest: str) -> bool:
    return (
        path.is_file() and path.stat().st_size == size
        and hashlib.sha256(path.read_bytes()).hexdigest() == digest
    )


def _stage_archive(
    target: Path, archive: Path | None, source: tuple[str, int, str],
) -> None:
    url, size, digest = source
    if archive is None:
        with urllib.request.urlopen(url, timeout=180) as response:
            target.write_bytes(response.read(size + 1))
    else:
        if not _valid(archive, size, digest):
            raise ValueError("提供的离线资源压缩包校验失败")
        shutil.copyfile(archive, target)
    if not _valid(target, size, digest):
        raise ValueError("资源压缩包校验失败")


def prepare_wordnet(data_dir: Path, archive: Path | None = None) -> Path:
    target = data_dir / "corpora" / "wordnet.zip"
    if _valid(target, *_WORDNET[1:]):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        candidate = Path(staging) / target.name
        _stage_archive(candidate, archive, _WORDNET)
        candidate.replace(target)
    return target


def prepare_tiktoken(data_dir: Path, vocabulary: Path | None = None) -> Path:
    key = hashlib.sha1(_CL100K[0].encode(), usedforsecurity=False).hexdigest()
    target = data_dir / "tiktoken" / key
    if _valid(target, *_CL100K[1:]):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        candidate = Path(staging) / key
        _stage_archive(candidate, vocabulary, _CL100K)
        candidate.replace(target)
    return target


def prepare_huqie(data_dir: Path, archive: Path | None = None) -> Path:
    target = data_dir / "ragflow" / "huqie.txt"
    if _valid(target, _HUQIE_BYTES, _HUQIE_SHA256):
        return target
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=target.parent) as staging:
        wheel = Path(staging) / "infinity-sdk.whl"
        _stage_archive(wheel, archive, _INFINITY_SDK)
        candidate = Path(staging) / target.name
        with ZipFile(wheel) as package, package.open("infinity/huqie.txt") as dictionary:
            candidate.write_bytes(dictionary.read(_HUQIE_BYTES + 1))
        if not _valid(candidate, _HUQIE_BYTES, _HUQIE_SHA256):
            raise ValueError("Infinity huqie 词典校验失败")
        candidate.replace(target)
    return target


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data-dir", type=Path, required=True)
    parser.add_argument("--wordnet-archive", type=Path)
    parser.add_argument("--infinity-sdk-archive", type=Path)
    parser.add_argument("--tiktoken-vocabulary", type=Path)
    args = parser.parse_args()
    print(prepare_tiktoken(args.data_dir, args.tiktoken_vocabulary))
    print(prepare_wordnet(args.data_dir, args.wordnet_archive))
    print(prepare_huqie(args.data_dir, args.infinity_sdk_archive))


if __name__ == "__main__":
    main()
