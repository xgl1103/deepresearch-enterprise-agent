"""Distributed per-user admission control backed by Redis."""

from __future__ import annotations

import os
import time
from dataclasses import dataclass

import redis.asyncio as redis

from agent.observability import QUOTA_REJECTIONS

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
PER_MINUTE = max(1, int(os.getenv("USER_RESEARCH_PER_MINUTE", "5")))
PER_DAY = max(PER_MINUTE, int(os.getenv("USER_RESEARCH_PER_DAY", "100")))
MAX_CONCURRENT = max(1, int(os.getenv("USER_CONCURRENT_RESEARCH", "2")))

_redis: redis.Redis | None = None

_ADMIT_SCRIPT = """
local minute_count = tonumber(redis.call('GET', KEYS[1]) or '0')
local day_count = tonumber(redis.call('GET', KEYS[2]) or '0')
local active_count = tonumber(redis.call('GET', KEYS[3]) or '0')
if minute_count >= tonumber(ARGV[1]) then return {0, 1, tonumber(ARGV[4])} end
if day_count >= tonumber(ARGV[2]) then return {0, 2, tonumber(ARGV[5])} end
if active_count >= tonumber(ARGV[3]) then return {0, 3, 30} end
redis.call('INCR', KEYS[1]); redis.call('EXPIRE', KEYS[1], tonumber(ARGV[4]))
redis.call('INCR', KEYS[2]); redis.call('EXPIRE', KEYS[2], tonumber(ARGV[5]))
redis.call('INCR', KEYS[3]); redis.call('EXPIRE', KEYS[3], tonumber(ARGV[5]))
return {1, 0, 0}
"""

_RELEASE_SCRIPT = """
local current = tonumber(redis.call('GET', KEYS[1]) or '0')
if current <= 1 then redis.call('DEL', KEYS[1]); return 0 end
return redis.call('DECR', KEYS[1])
"""


@dataclass(frozen=True)
class AdmissionResult:
    allowed: bool
    reason: str = ""
    retry_after: int = 0


async def _get_redis() -> redis.Redis:
    global _redis
    if _redis is None:
        _redis = redis.from_url(REDIS_URL, decode_responses=True)
    return _redis


async def admit_research(user_id: int) -> AdmissionResult:
    """Atomically enforce minute, day, and concurrent task limits."""
    now = int(time.time())
    minute_ttl = 60 - now % 60
    day_ttl = 86400 - now % 86400
    prefix = f"research:quota:{user_id}"
    result = await (await _get_redis()).eval(
        _ADMIT_SCRIPT,
        3,
        f"{prefix}:minute:{now // 60}",
        f"{prefix}:day:{now // 86400}",
        f"{prefix}:active",
        PER_MINUTE,
        PER_DAY,
        MAX_CONCURRENT,
        minute_ttl,
        day_ttl,
    )
    allowed, reason_code, retry_after = [int(item) for item in result]
    reasons = {1: "minute", 2: "day", 3: "concurrent"}
    reason = reasons.get(reason_code, "")
    if not allowed:
        QUOTA_REJECTIONS.labels(reason or "unknown").inc()
    return AdmissionResult(bool(allowed), reason, retry_after)


async def release_research_slot(user_id: int) -> None:
    """Release one active slot after a task phase reaches a terminal state."""
    if not user_id:
        return
    await (await _get_redis()).eval(
        _RELEASE_SCRIPT, 1, f"research:quota:{user_id}:active"
    )
