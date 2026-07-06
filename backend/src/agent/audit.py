"""Append-only audit logging with failure isolation."""

from __future__ import annotations

from loguru import logger

from agent.db.engine import get_session_factory
from agent.db.models import AuditEvent
from agent.observability import request_id_var


async def write_audit_event(
    action: str,
    resource_type: str,
    outcome: str,
    *,
    user_id: int | None = None,
    resource_id: str | None = None,
    details: dict | None = None,
) -> None:
    """Append an audit event; storage failure does not change business output."""
    try:
        async with get_session_factory()() as session:
            session.add(AuditEvent(
                user_id=user_id,
                action=action,
                resource_type=resource_type,
                resource_id=resource_id,
                outcome=outcome,
                request_id=request_id_var.get(),
                details=details or {},
            ))
            await session.commit()
    except Exception as exc:
        logger.exception(f"[Audit] 审计事件写入失败 action={action}: {exc}")
