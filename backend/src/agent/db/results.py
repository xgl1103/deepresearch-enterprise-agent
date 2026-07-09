"""Persistence helpers for durable research results."""

from __future__ import annotations

from sqlalchemy import desc, select

from agent.db.engine import get_session_factory
from agent.db.models import ResearchResult, UserThread


async def save_research_result(
    thread_id: str,
    status: str,
    report: str | None = None,
    sources: list[dict] | None = None,
    error_type: str | None = None,
) -> None:
    """Insert or update the single durable result row for a thread."""
    async with get_session_factory()() as session:
        result = await session.execute(
            select(ResearchResult).where(ResearchResult.thread_id == thread_id)
        )
        row = result.scalar_one_or_none()
        if row is None:
            row = ResearchResult(thread_id=thread_id, status=status, sources=sources or [])
            session.add(row)
        else:
            row.status = status
            if sources is not None:
                row.sources = sources
        if report is not None:
            row.report = report
        row.error_type = error_type
        await session.commit()


async def get_research_result(thread_id: str) -> ResearchResult | None:
    """Load a durable result by thread identifier."""
    async with get_session_factory()() as session:
        result = await session.execute(
            select(ResearchResult).where(ResearchResult.thread_id == thread_id)
        )
        return result.scalar_one_or_none()


async def list_user_research_history(user_id: int, limit: int = 20) -> list[dict]:
    """Load the current user's recent research threads with durable result metadata."""
    safe_limit = max(1, min(limit, 50))
    async with get_session_factory()() as session:
        result = await session.execute(
            select(UserThread, ResearchResult)
            .outerjoin(ResearchResult, ResearchResult.thread_id == UserThread.thread_id)
            .where(UserThread.user_id == user_id)
            .order_by(desc(UserThread.created_at))
            .limit(safe_limit)
        )
        items: list[dict] = []
        for thread, research_result in result.all():
            items.append({
                "task_id": thread.thread_id,
                "title": thread.title or "未命名研究",
                "created_at": thread.created_at.isoformat() if thread.created_at else None,
                "status": research_result.status if research_result else "queued",
                "report": research_result.report if research_result else None,
                "sources": research_result.sources if research_result else [],
                "error_type": research_result.error_type if research_result else None,
                "updated_at": (
                    research_result.updated_at.isoformat()
                    if research_result and research_result.updated_at
                    else None
                ),
            })
        return items
