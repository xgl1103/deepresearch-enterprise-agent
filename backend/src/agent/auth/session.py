"""Redis 会话管理.

会话数据:
    key:   session:{session_id} → hash {
        "user_id": str,
        "username": str,
        "last_active": iso8601,
    }
    ttl:   86400 秒（24 小时），每次访问续期
"""
from __future__ import annotations

import os
import secrets
from datetime import datetime, timezone

import redis.asyncio as redis

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
SESSION_IDLE_TTL = max(300, int(os.getenv("SESSION_IDLE_TTL_SECONDS", "3600")))
SESSION_ABSOLUTE_TTL = max(
    SESSION_IDLE_TTL,
    int(os.getenv("SESSION_ABSOLUTE_TTL_SECONDS", "86400")),
)
SESSION_PREFIX = "session:"
USER_SESSIONS_PREFIX = "user-sessions:"
SESSION_COOKIE_NAME = os.getenv("SESSION_COOKIE_NAME", "session_id")

_redis: redis.Redis | None = None


async def _get_session_redis() -> redis.Redis:
    """获取会话 Redis 连接（与 task_queue 共享同一 Redis）."""
    global _redis
    if _redis is None:
        _redis = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            socket_timeout=5,
        )
    return _redis


async def create_session(user_id: int, username: str) -> str:
    """创建新会话，返回 session_id."""
    session_id = secrets.token_urlsafe(32)
    now = datetime.now(timezone.utc).isoformat()
    r = await _get_session_redis()
    key = f"{SESSION_PREFIX}{session_id}"
    await r.hset(key, mapping={
        "user_id": str(user_id),
        "username": username,
        "created_at": now,
        "last_active": now,
    })
    await r.expire(key, SESSION_IDLE_TTL)
    user_sessions_key = f"{USER_SESSIONS_PREFIX}{user_id}"
    await r.sadd(user_sessions_key, session_id)
    await r.expire(user_sessions_key, SESSION_ABSOLUTE_TTL)
    return session_id


async def get_session(session_id: str) -> dict | None:
    """获取并续期会话，返回会话数据或 None."""
    if not session_id:
        return None
    r = await _get_session_redis()
    key = f"{SESSION_PREFIX}{session_id}"
    data = await r.hgetall(key)
    if not data:
        return None
    now_dt = datetime.now(timezone.utc)
    try:
        created_at = datetime.fromisoformat(data["created_at"])
    except (KeyError, TypeError, ValueError):
        # 旧格式会话缺少绝对生命周期信息，要求重新登录。
        await r.delete(key)
        return None
    absolute_remaining = int(
        SESSION_ABSOLUTE_TTL - (now_dt - created_at).total_seconds()
    )
    if absolute_remaining <= 0:
        await r.delete(key)
        return None
    now = now_dt.isoformat()
    await r.hset(key, "last_active", now)
    await r.expire(key, min(SESSION_IDLE_TTL, absolute_remaining))
    data["last_active"] = now
    return data


async def delete_session(session_id: str) -> None:
    """删除会话（用户登出时调用）."""
    if not session_id:
        return
    r = await _get_session_redis()
    key = f"{SESSION_PREFIX}{session_id}"
    user_id = await r.hget(key, "user_id")
    await r.delete(key)
    if user_id:
        await r.srem(f"{USER_SESSIONS_PREFIX}{user_id}", session_id)


async def delete_user_sessions(user_id: int) -> None:
    """Invalidate every active session for a user after credential changes."""
    r = await _get_session_redis()
    index_key = f"{USER_SESSIONS_PREFIX}{user_id}"
    session_ids = await r.smembers(index_key)
    if session_ids:
        await r.delete(*[f"{SESSION_PREFIX}{sid}" for sid in session_ids])
    await r.delete(index_key)
