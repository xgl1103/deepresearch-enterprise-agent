"""Persistence helpers for durable research results."""

from __future__ import annotations

from sqlalchemy import select

from agent.db.engine import get_session_factory
from agent.db.models import ResearchResult


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
