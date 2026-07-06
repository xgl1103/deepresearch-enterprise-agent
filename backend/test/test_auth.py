"""Authentication, session lifetime, and resource isolation tests."""

import json
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from starlette.requests import Request


def _json_request(path: str, body: dict, user_id: int = 1) -> Request:
    encoded = json.dumps(body).encode()
    consumed = False

    async def receive():
        nonlocal consumed
        if consumed:
            return {"type": "http.request", "body": b"", "more_body": False}
        consumed = True
        return {"type": "http.request", "body": encoded, "more_body": False}

    request = Request(
        {
            "type": "http",
            "method": "POST",
            "path": path,
            "headers": [(b"content-type", b"application/json")],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 123),
            "scheme": "http",
        },
        receive,
    )
    request.state.user_id = user_id
    return request


class TestSessionLifetime:
    """Verify idle and absolute session expiration semantics."""

    @pytest.mark.asyncio
    async def test_create_session_records_creation_time(self):
        from agent.auth.session import create_session

        redis = MagicMock()
        redis.hset = AsyncMock()
        redis.expire = AsyncMock()
        redis.sadd = AsyncMock()
        with patch("agent.auth.session._get_session_redis", return_value=redis):
            session_id = await create_session(7, "alice")

        assert session_id
        mapping = redis.hset.await_args.kwargs["mapping"]
        assert mapping["user_id"] == "7"
        assert mapping["created_at"] == mapping["last_active"]

    @pytest.mark.asyncio
    async def test_delete_user_sessions_revokes_every_indexed_session(self):
        from agent.auth.session import delete_user_sessions

        redis = MagicMock()
        redis.smembers = AsyncMock(return_value={"one", "two"})
        redis.delete = AsyncMock()
        with patch("agent.auth.session._get_session_redis", return_value=redis):
            await delete_user_sessions(7)

        deleted_keys = redis.delete.await_args_list[0].args
        assert set(deleted_keys) == {"session:one", "session:two"}

    @pytest.mark.asyncio
    async def test_absolute_expired_session_is_deleted(self, monkeypatch):
        import agent.auth.session as sessions

        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value={
            "user_id": "7",
            "username": "alice",
            "created_at": (
                datetime.now(timezone.utc) - timedelta(hours=2)
            ).isoformat(),
            "last_active": datetime.now(timezone.utc).isoformat(),
        })
        redis.delete = AsyncMock()
        monkeypatch.setattr(sessions, "SESSION_ABSOLUTE_TTL", 3600)
        with patch("agent.auth.session._get_session_redis", return_value=redis):
            result = await sessions.get_session("expired")

        assert result is None
        redis.delete.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_legacy_session_without_created_at_is_rejected(self):
        from agent.auth.session import get_session

        redis = MagicMock()
        redis.hgetall = AsyncMock(return_value={
            "user_id": "7",
            "username": "alice",
        })
        redis.delete = AsyncMock()
        with patch("agent.auth.session._get_session_redis", return_value=redis):
            result = await get_session("legacy")

        assert result is None
        redis.delete.assert_awaited_once()


class TestThreadIsolation:
    """Verify task identifiers cannot cross user boundaries."""

    @pytest.mark.asyncio
    async def test_resume_foreign_task_is_hidden_and_never_enqueued(self):
        from agent.app import submit_research

        request = _json_request(
            "/api/research",
            {
                "task_id": "another-users-task",
                "messages": [{"type": "human", "content": "resume"}],
            },
            user_id=42,
        )
        with (
            patch("agent.app.user_owns_thread", return_value=False),
            patch("agent.app.enqueue_task", new_callable=AsyncMock) as enqueue,
            patch("agent.app.write_audit_event", new_callable=AsyncMock) as audit,
        ):
            response = await submit_research(request)

        assert response.status_code == 404
        enqueue.assert_not_awaited()
        audit.assert_awaited_once()
        assert audit.await_args.args[:3] == (
            "research_submit",
            "research_thread",
            "denied",
        )


class TestAuthMiddlewareFailures:
    """Verify infrastructure failures do not become unstructured 500 errors."""

    @pytest.mark.asyncio
    async def test_redis_failure_returns_503(self):
        from agent.auth.middleware import AuthMiddleware

        request = Request({
            "type": "http",
            "method": "GET",
            "path": "/api/research/task/stream",
            "headers": [(b"cookie", b"session_id=abc")],
            "query_string": b"",
            "server": ("test", 80),
            "client": ("test", 123),
            "scheme": "http",
        })
        middleware = AuthMiddleware(app=MagicMock())
        with patch("agent.auth.middleware.get_session", side_effect=RuntimeError("down")):
            response = await middleware.dispatch(request, AsyncMock())

        assert response.status_code == 503


class TestNativeLangGraphAuthorization:
    """Verify native LangGraph resources use owner-scoped authorization."""

    @pytest.mark.asyncio
    async def test_thread_creation_stamps_authenticated_owner(self):
        from agent.auth.langgraph_auth import create_owned_thread

        ctx = MagicMock()
        ctx.user.identity = "alice"
        value = {}
        await create_owned_thread(ctx, value)
        assert value["metadata"]["owner"] == "alice"

    @pytest.mark.asyncio
    async def test_thread_access_returns_owner_filter(self):
        from agent.auth.langgraph_auth import read_owned_thread

        ctx = MagicMock()
        ctx.user.identity = "alice"
        assert await read_owned_thread(ctx, {}) == {"owner": "alice"}

    def test_langgraph_config_references_custom_auth(self):
        from pathlib import Path

        config_path = Path(__file__).parents[1] / "langgraph.json"
        config = json.loads(config_path.read_text(encoding="utf-8"))
        assert config["auth"]["path"].endswith("langgraph_auth.py:auth")
