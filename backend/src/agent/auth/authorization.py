"""Resource ownership checks shared by research endpoints."""

from sqlalchemy import select

from agent.db.engine import get_session_factory
from agent.db.models import UserThread


async def user_owns_thread(user_id: int, thread_id: str) -> bool:
    """Return whether the thread belongs exclusively to the user."""
    async with get_session_factory()() as session:
        result = await session.execute(
            select(UserThread.id).where(
                UserThread.user_id == user_id,
                UserThread.thread_id == thread_id,
            )
        )
        return result.scalar_one_or_none() is not None
