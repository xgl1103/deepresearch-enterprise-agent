"""Metrics and correlation context for API, worker, search, and LLM calls."""

from __future__ import annotations

import json
import os
from contextvars import ContextVar

from prometheus_client import Counter, Gauge, Histogram

request_id_var: ContextVar[str] = ContextVar("request_id", default="-")
task_id_var: ContextVar[str] = ContextVar("task_id", default="-")

HTTP_REQUESTS = Counter(
    "deepresearch_http_requests_total",
    "HTTP requests",
    ("method", "route", "status"),
)
HTTP_DURATION = Histogram(
    "deepresearch_http_request_duration_seconds",
    "HTTP request latency",
    ("method", "route"),
)
HTTP_ACTIVE = Gauge("deepresearch_http_active_requests", "Active HTTP requests")
TASK_TRANSITIONS = Counter(
    "deepresearch_task_transitions_total",
    "Task lifecycle transitions",
    ("status",),
)
TASK_DURATION = Histogram(
    "deepresearch_task_duration_seconds",
    "Worker task delivery duration",
    ("outcome",),
)
LLM_REQUESTS = Counter(
    "deepresearch_llm_requests_total", "LLM requests", ("model", "outcome")
)
LLM_TOKENS = Counter(
    "deepresearch_llm_tokens_total", "LLM token usage", ("model", "type")
)
LLM_ESTIMATED_COST = Counter(
    "deepresearch_llm_estimated_cost_currency_total",
    "Estimated LLM cost in the configured pricing currency",
    ("model",),
)
QUOTA_REJECTIONS = Counter(
    "deepresearch_quota_rejections_total",
    "Rejected research submissions",
    ("reason",),
)
TASK_QUEUE_DEPTH = Gauge("deepresearch_task_queue_depth", "Queued task stream length")
TASK_QUEUE_PENDING = Gauge(
    "deepresearch_task_queue_pending", "Tasks delivered but not acknowledged"
)


def _pricing() -> dict[str, dict[str, float]]:
    raw = os.getenv("MODEL_PRICING_JSON", "{}").strip() or "{}"
    try:
        value = json.loads(raw)
        return value if isinstance(value, dict) else {}
    except (json.JSONDecodeError, TypeError):
        return {}


def record_llm_usage(
    model: str,
    prompt_tokens: int = 0,
    completion_tokens: int = 0,
) -> None:
    """Record provider-reported usage and a configurable cost estimate."""
    model = model or "unknown"
    prompt_tokens = max(0, int(prompt_tokens or 0))
    completion_tokens = max(0, int(completion_tokens or 0))
    LLM_TOKENS.labels(model, "prompt").inc(prompt_tokens)
    LLM_TOKENS.labels(model, "completion").inc(completion_tokens)
    prices = _pricing().get(model, {})
    estimated = (
        prompt_tokens * float(prices.get("input_per_million", 0))
        + completion_tokens * float(prices.get("output_per_million", 0))
    ) / 1_000_000
    if estimated:
        LLM_ESTIMATED_COST.labels(model).inc(estimated)
