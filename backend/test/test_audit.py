"""Audit logging tests."""

from unittest.mock import patch

import pytest


class _FakeSession:
    def __init__(self, *, fail_commit: bool = False):
        self.fail_commit = fail_commit
        self.added = []
        self.committed = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    def add(self, value):
        self.added.append(value)

    async def commit(self):
        if self.fail_commit:
            raise RuntimeError("database down")
        self.committed = True


@pytest.mark.asyncio
async def test_write_audit_event_persists_safe_details():
    from agent.audit import write_audit_event

    session = _FakeSession()
    with patch("agent.audit.get_session_factory", return_value=lambda: session):
        await write_audit_event(
            "password_change",
            "user",
            "denied",
            user_id=7,
            resource_id="7",
            details={"reason": "bad_current_password"},
        )

    assert session.committed is True
    assert len(session.added) == 1
    event = session.added[0]
    assert event.action == "password_change"
    assert event.outcome == "denied"
    assert event.user_id == 7
    assert event.details == {"reason": "bad_current_password"}
    assert "password" not in str(event.details).replace("bad_current_password", "")


@pytest.mark.asyncio
async def test_write_audit_event_failure_is_isolated():
    from agent.audit import write_audit_event

    session = _FakeSession(fail_commit=True)
    with patch("agent.audit.get_session_factory", return_value=lambda: session):
        await write_audit_event("login", "session", "success", user_id=1)

    assert len(session.added) == 1
