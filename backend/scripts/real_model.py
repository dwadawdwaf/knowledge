"""读取真实模型配置，只向临时运行环境传递必要的模型字段。"""

from __future__ import annotations

import pathlib
from urllib.parse import urlsplit

from pydantic import ValidationError
from pydantic_settings import SettingsConfigDict

from knowledge.config import Settings
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
        "similarity_floor",
        "semantic_max_distance",
        "evidence_min_score",
        "evidence_max_items",
        "lease_seconds",
        "heartbeat_seconds",
        "max_attempts",
        "document_parser_profile",
        "pdf_parser_profile",
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
