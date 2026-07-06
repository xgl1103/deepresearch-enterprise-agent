"""Runtime configuration and secret-loading tests."""

import pytest


def _valid_env(monkeypatch) -> None:
    monkeypatch.setenv("APP_TOKEN", "valid-token")
    monkeypatch.setenv("MCP_APP_ID", "valid-app")
    monkeypatch.setenv("LLM_BASE_URL", "https://llm.example.com/v1")
    monkeypatch.setenv("REDIS_URL", "redis://localhost:6379/0")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://user:strong@localhost:5432/deepresearch",
    )
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("METRICS_TOKEN", "metrics-test-token")


def test_valid_development_config(monkeypatch):
    from agent.runtime_config import validate_runtime_config

    _valid_env(monkeypatch)
    validate_runtime_config("api")


def test_missing_required_secret_fails_fast(monkeypatch):
    from agent.runtime_config import ConfigurationError, validate_runtime_config

    _valid_env(monkeypatch)
    monkeypatch.delenv("APP_TOKEN")
    with pytest.raises(ConfigurationError, match="APP_TOKEN"):
        validate_runtime_config("worker")


def test_file_backed_secret_is_loaded(monkeypatch, tmp_path):
    from agent.runtime_config import validate_runtime_config

    _valid_env(monkeypatch)
    monkeypatch.delenv("APP_TOKEN")
    secret_file = tmp_path / "app_token"
    secret_file.write_text("from-secret-file\n", encoding="utf-8")
    monkeypatch.setenv("APP_TOKEN_FILE", str(secret_file))

    validate_runtime_config("api")
    assert __import__("os").environ["APP_TOKEN"] == "from-secret-file"


def test_secret_and_secret_file_are_mutually_exclusive(monkeypatch, tmp_path):
    from agent.runtime_config import ConfigurationError, validate_runtime_config

    _valid_env(monkeypatch)
    secret_file = tmp_path / "app_token"
    secret_file.write_text("file-value", encoding="utf-8")
    monkeypatch.setenv("APP_TOKEN_FILE", str(secret_file))
    with pytest.raises(ConfigurationError, match="不能同时配置"):
        validate_runtime_config("api")


def test_production_rejects_insecure_cookie(monkeypatch):
    from agent.runtime_config import ConfigurationError, validate_runtime_config

    _valid_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "false")
    with pytest.raises(ConfigurationError, match="SESSION_COOKIE_SECURE"):
        validate_runtime_config("api")


def test_production_rejects_embedded_worker(monkeypatch):
    from agent.runtime_config import ConfigurationError, validate_runtime_config

    _valid_env(monkeypatch)
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_COOKIE_SECURE", "true")
    monkeypatch.setenv("EMBEDDED_TASK_WORKER", "true")
    with pytest.raises(ConfigurationError, match="EMBEDDED_TASK_WORKER"):
        validate_runtime_config("api")


def test_invalid_model_pricing_fails_startup(monkeypatch):
    from agent.runtime_config import ConfigurationError, validate_runtime_config

    _valid_env(monkeypatch)
    monkeypatch.setenv(
        "MODEL_PRICING_JSON",
        '{"model":{"input_per_million":"not-a-number"}}',
    )
    with pytest.raises(ConfigurationError, match="MODEL_PRICING_JSON"):
        validate_runtime_config("api")
