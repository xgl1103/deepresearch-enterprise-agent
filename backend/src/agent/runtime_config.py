"""Central startup validation and file-backed secret loading."""

from __future__ import annotations

import json
import os
from pathlib import Path
from urllib.parse import urlparse

from dotenv import load_dotenv

load_dotenv()

SECRET_NAMES = (
    "APP_TOKEN",
    "MCP_APP_ID",
    "EMBEDDING_API_KEY",
    "RERANKER_API_KEY",
    "BOOTSTRAP_PASSWORD",
    "METRICS_TOKEN",
)


class ConfigurationError(RuntimeError):
    """Raised when runtime configuration is missing or unsafe."""


def load_file_backed_secrets() -> None:
    """Load NAME_FILE contents into NAME without ever logging secret values."""
    for name in SECRET_NAMES:
        file_var = f"{name}_FILE"
        secret_file = os.getenv(file_var, "").strip()
        if not secret_file:
            continue
        if os.getenv(name):
            raise ConfigurationError(f"{name} 与 {file_var} 不能同时配置")
        path = Path(secret_file)
        try:
            if path.stat().st_size > 65536:
                raise ConfigurationError(f"{file_var} 文件过大")
            value = path.read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigurationError(f"无法读取 {file_var}: {exc}") from exc
        if not value:
            raise ConfigurationError(f"{file_var} 指向空文件")
        os.environ[name] = value


def _require_url(name: str, allowed_schemes: set[str]) -> None:
    value = os.getenv(name, "").strip()
    parsed = urlparse(value)
    if not value or parsed.scheme not in allowed_schemes or not parsed.hostname:
        schemes = "/".join(sorted(allowed_schemes))
        raise ConfigurationError(f"{name} 必须是有效的 {schemes} URL")


def validate_runtime_config(component: str = "api") -> None:
    """Fail fast on missing, malformed, or production-unsafe settings."""
    load_file_backed_secrets()
    missing = [
        name for name in ("APP_TOKEN", "MCP_APP_ID", "LLM_BASE_URL")
        if not os.getenv(name, "").strip()
    ]
    if missing:
        raise ConfigurationError(f"缺少必需配置: {', '.join(missing)}")

    _require_url("LLM_BASE_URL", {"http", "https"})
    _require_url("REDIS_URL", {"redis", "rediss"})
    _require_url("DATABASE_URL", {"postgresql+asyncpg"})

    models = os.getenv("AVAILABLE_MODELS", "").strip()
    if models:
        try:
            parsed_models = json.loads(models)
        except json.JSONDecodeError as exc:
            raise ConfigurationError("AVAILABLE_MODELS 必须是合法 JSON") from exc
        if not isinstance(parsed_models, list) or not parsed_models:
            raise ConfigurationError("AVAILABLE_MODELS 必须是非空数组")

    pricing_raw = os.getenv("MODEL_PRICING_JSON", "{}").strip() or "{}"
    try:
        pricing = json.loads(pricing_raw)
        if not isinstance(pricing, dict):
            raise TypeError
        for model_prices in pricing.values():
            if not isinstance(model_prices, dict):
                raise TypeError
            for key in ("input_per_million", "output_per_million"):
                value = float(model_prices.get(key, 0))
                if value < 0:
                    raise ValueError
    except (json.JSONDecodeError, TypeError, ValueError) as exc:
        raise ConfigurationError("MODEL_PRICING_JSON 格式或价格无效") from exc

    for name in (
        "USER_RESEARCH_PER_MINUTE",
        "USER_RESEARCH_PER_DAY",
        "USER_CONCURRENT_RESEARCH",
        "MAX_INITIAL_SEARCH_QUERIES",
        "MAX_RESEARCH_LOOPS_ALLOWED",
    ):
        raw = os.getenv(name)
        if raw is not None:
            try:
                if int(raw) < 1:
                    raise ValueError
            except ValueError as exc:
                raise ConfigurationError(f"{name} 必须是正整数") from exc

    environment = os.getenv("APP_ENV", "development").lower()
    if environment == "production":
        if not os.getenv("METRICS_TOKEN", "").strip():
            raise ConfigurationError("生产环境必须配置 METRICS_TOKEN")
        if os.getenv("SESSION_COOKIE_SECURE", "false").lower() not in {
            "1", "true", "yes"
        }:
            raise ConfigurationError("生产环境必须设置 SESSION_COOKIE_SECURE=true")
        if os.getenv("EMBEDDED_TASK_WORKER", "false").lower() in {
            "1", "true", "yes"
        }:
            raise ConfigurationError("生产环境禁止启用 EMBEDDED_TASK_WORKER")
        database_url = os.getenv("DATABASE_URL", "")
        if ":postgres@" in database_url:
            raise ConfigurationError("生产环境禁止使用默认 PostgreSQL 密码")
        if not urlparse(os.getenv("REDIS_URL", "")).password:
            raise ConfigurationError("生产环境 Redis 必须配置密码")

    if component not in {"api", "worker"}:
        raise ConfigurationError(f"未知运行组件: {component}")
