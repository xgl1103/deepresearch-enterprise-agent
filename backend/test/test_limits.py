"""Distributed user quota tests."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest


@pytest.mark.asyncio
async def test_admission_allowed():
    from agent.limits import admit_research

    redis = MagicMock()
    redis.eval = AsyncMock(return_value=[1, 0, 0])
    with patch("agent.limits._get_redis", return_value=redis):
        result = await admit_research(7)

    assert result.allowed is True
    assert redis.eval.await_args.args[1] == 3


@pytest.mark.asyncio
async def test_concurrent_limit_returns_retry_hint():
    from agent.limits import admit_research

    redis = MagicMock()
    redis.eval = AsyncMock(return_value=[0, 3, 30])
    with patch("agent.limits._get_redis", return_value=redis):
        result = await admit_research(7)

    assert result.allowed is False
    assert result.reason == "concurrent"
    assert result.retry_after == 30


@pytest.mark.asyncio
async def test_release_uses_atomic_non_negative_script():
    from agent.limits import release_research_slot

    redis = MagicMock()
    redis.eval = AsyncMock(return_value=0)
    with patch("agent.limits._get_redis", return_value=redis):
        await release_research_slot(7)

    assert redis.eval.await_args.args[1:] == (1, "research:quota:7:active")
