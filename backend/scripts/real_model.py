"""读取真实模型配置，只向临时运行环境传递必要的模型字段。"""

from __future__ import annotations

import os
import pathlib
from urllib.parse import urlsplit

from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from knowledge.config import Settings
from knowledge.domain.errors import KeywordIndexUnavailable
from knowledge.repositories.tables import EMBEDDING_DIM

BACKEND = pathlib.Path(__file__).resolve().parents[1]


class _RealSettings(Settings):
    model_config = SettingsConfigDict(
        env_prefix="KS_", env_file=BACKEND / ".env", extra="ignore", env_ignore_empty=True
    )


def validate_real_settings(settings: Settings) -> None:
    for name, endpoint in (
        ("向量", settings.embedding_base_url),
        ("生成", settings.generator_base_url),
    ):
        if not endpoint:
            raise ValueError(f"缺少{name}模型端点，请配置 backend/.env；不会回退到模型桩")
        parsed = urlsplit(endpoint)
        if (
            parsed.scheme not in ("http", "https")
            or not parsed.hostname
            or parsed.username
            or parsed.password
        ):
            raise ValueError(f"{name}模型端点格式不正确，请将认证信息放在 API_KEY 字段")
    if not settings.embedding_model or settings.embedding_model == "hash-1024":
        raise ValueError("真实模式需要明确的向量模型，不能使用 hash-1024")
    if not settings.generator_model or settings.generator_model == "stub":
        raise ValueError("真实模式需要明确的生成模型，不能使用 stub")
    if settings.embedding_dim != EMBEDDING_DIM:
        raise ValueError(f"当前数据库要求 {EMBEDDING_DIM} 维向量，不允许混用维度")


def load_real_settings() -> Settings:
    try:
        settings = _RealSettings()
    except ValidationError as exc:
        raise ValueError(
            "backend/.env 配置格式不正确，请检查字段类型；不输出可能包含密钥的错误详情"
        ) from exc
    validate_real_settings(settings)
    return settings


def ragflow_resources_dir() -> pathlib.Path:
    """词表资源目录：仓库随包资源优先，未入库时退回本地准备目录。"""
    bundled = BACKEND / "resources" / "ragflow"
    if (bundled / "ragflow" / "huqie.txt").is_file():
        return bundled
    return BACKEND / ".data" / "ragflow-resources"


def check_ragflow_resources() -> None:
    """用固定短文本检查实际分词与计数资源，适配器只读取本地文件。"""
    import nltk.data

    from knowledge.adapters.ragflow_text import count_tokens, query_terms

    # 启动预检和 LocalRuntime 均调用此入口，后者将绝对路径传给 API/Worker。
    # NLTK 可能在入口装配时已被导入，修改环境后也须同步它已初始化的搜索路径。
    resources = ragflow_resources_dir()
    os.environ.setdefault("NLTK_DATA", str(resources))
    if "TIKTOKEN_CACHE_DIR" not in os.environ and "DATA_GYM_CACHE_DIR" not in os.environ:
        os.environ["TIKTOKEN_CACHE_DIR"] = str(resources / "tiktoken")
    for directory in reversed(os.environ["NLTK_DATA"].split(os.pathsep)):
        if directory:
            resolved = str(pathlib.Path(directory).resolve())
            if resolved not in nltk.data.path:
                nltk.data.path.insert(0, resolved)
    try:
        count_tokens("本地词表")
    except (KeywordIndexUnavailable, OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            "cl100k_base 词表缺失或校验失败。仓库资源应位于 backend/resources/ragflow，"
            "缺失时可运行 backend/scripts/prepare_ragflow_resources.py --data-dir <目录>；"
            "若手动配置了 TIKTOKEN_CACHE_DIR 或 DATA_GYM_CACHE_DIR，请检查该目录。"
        ) from exc
    try:
        query_terms("压力 pressure")
    except (KeywordIndexUnavailable, OSError, RuntimeError, ValueError) as exc:
        raise ValueError(
            "WordNet 或 huqie 本地词典不可用。仓库资源应位于 backend/resources/ragflow，"
            "缺失时可运行 backend/scripts/prepare_ragflow_resources.py --data-dir <目录>；"
            "若手动配置了 NLTK_DATA，请检查该目录。"
        ) from exc


def external_database_url(settings: Settings) -> str | None:
    """显式配置了 KS_DATABASE_URL 时返回该地址，否则返回 None（沿用临时库）。

    只有写进 `.env` 才会出现在 model_fields_set 里，配置类自带的默认值不触发外部库。
    """
    if "database_url" in settings.model_fields_set:
        return settings.database_url
    return None


def external_database_url_from_file() -> str | None:
    """不校验模型配置，只读取 backend/.env 中显式配置的数据库地址。"""
    return external_database_url(_RealSettings())


def smoke_database_url_from_file() -> str | None:
    """浏览器验收专用库；未配置时返回 None，退回临时库。"""
    return _RealSettings().smoke_database_url


def real_model_environment(settings: Settings) -> dict[str, str]:
    validate_real_settings(settings)
    fields = (
        "embedding_base_url",
        "embedding_model",
        "embedding_dim",
        "embedding_batch",
        "embedding_api_key",
        "embedding_dimensions",
        "generator_base_url",
        "generator_model",
        "generator_api_key",
        "generator_timeout_seconds",
        "evidence_min_score",
        "evidence_max_items",
        "retrieval_profile",
        "search_engine",
        "opensearch_url",
        "opensearch_index",
        "opensearch_timeout_seconds",
        "evidence_candidate_limit",
        "evidence_max_chars",
        "lease_seconds",
        "heartbeat_seconds",
        "max_attempts",
        "event_stream_limit",
        "document_parser_profile",
        "mineru_url",
        "mineru_token",
        "mineru_timeout_seconds",
        "mineru_output_bytes",
        "parse_output_bytes",
    )
    # 不透传业务库、文件卷或身份密钥，避免演示配置意外触及现有数据。
    return {
        "KS_" + name.upper(): str(getattr(settings, name))
        for name in fields
        if getattr(settings, name) is not None
    }
