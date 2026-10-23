"""适配器：外部系统的实现。业务判断不写在这里。"""

from knowledge.adapters.embedding import HashEmbedder, HttpEmbedder, build_embedder
from knowledge.adapters.generator import HttpGenerator, StubGenerator, build_generator
from knowledge.adapters.parser import (
    DocxParser,
    PdfParser,
    TextParser,
    build_parser_registry,
    detect_media_type,
)
from knowledge.adapters.storage import LocalStorage

__all__ = [
    "DocxParser",
    "HashEmbedder",
    "HttpEmbedder",
    "HttpGenerator",
    "LocalStorage",
    "PdfParser",
    "StubGenerator",
    "TextParser",
    "build_embedder",
    "build_generator",
    "build_parser_registry",
    "detect_media_type",
]
