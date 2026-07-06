"""Tests for task_queue — Redis Streams 任务队列模块.

测试覆盖：
  - 导入验证（无导入错误）
  - enqueue_task 入队逻辑（mock Redis）
  - read_task_events 事件读取（mock Redis）
  - 终止事件检测（error / finalize_answer / task_paused）
"""

import json
import uuid
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


# ═══════════════════════════════════════════════════════════════════════
# 导入验证
# ═══════════════════════════════════════════════════════════════════════

class TestTaskQueueImport:
    def test_import_succeeds(self):
        """验证 task_queue 模块可以正常导入。"""
        from agent.task_queue import (
            enqueue_task,
            start_worker,
            read_task_events,
            TASK_STREAM,
            EVENT_STREAM_PREFIX,
            CONSUMER_GROUP,
        )
        assert TASK_STREAM == "research:tasks"
        assert EVENT_STREAM_PREFIX == "research:events"
        assert CONSUMER_GROUP == "research-workers"


class TestBuildInitialState:
    def test_copies_frontend_reasoning_model_into_graph_state(self):
        from agent.task_queue import _build_initial_state

        messages = [MagicMock()]
        state = _build_initial_state(
            {
                "plan_status": "unconfirmed",
                "plan": "",
                "reasoning_model": "qwen-selected-by-user",
                "initial_search_query_count": 3,
                "max_research_loops": 4,
            },
            messages,
        )

        assert state["messages"] is messages
        assert state["reasoning_model"] == "qwen-selected-by-user"
        assert state["initial_search_query_count"] == 3
        assert state["max_research_loops"] == 4
        assert state["research_loop_count"] == 0

    def test_preserves_reasoning_model_when_resuming_confirmed_plan(self):
        from agent.task_queue import _build_initial_state

        state = _build_initial_state(
            {
                "plan_status": "confirmed",
                "plan": "# confirmed plan",
                "reasoning_model": "qwen-selected-by-user",
            },
            [MagicMock()],
        )

        assert state["plan_status"] == "confirmed"
        assert state["plan"] == "# confirmed plan"
        assert state["reasoning_model"] == "qwen-selected-by-user"


# ═══════════════════════════════════════════════════════════════════════
# enqueue_task 测试
# ═══════════════════════════════════════════════════════════════════════

class TestEnqueueTask:
    @pytest.mark.asyncio
    async def test_enqueue_returns_task_id(self):
        """验证入队返回有效的 task_id。"""
        from agent.task_queue import enqueue_task, _get_redis

        mock_redis = MagicMock()
        mock_redis.xadd = AsyncMock()
        mock_redis.delete = AsyncMock()
        mock_redis.set = AsyncMock()

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            task_id = await enqueue_task(
                messages=[{"type": "human", "content": "测试"}],
                initial_search_query_count=2,
                max_research_loops=2,
                reasoning_model="test-model",
            )

        # 验证返回的是有效的 UUID 格式
        assert isinstance(task_id, str)
        assert len(task_id) == 36  # UUID 标准长度

    @pytest.mark.asyncio
    async def test_enqueue_calls_xadd_with_payload(self):
        """验证入队时正确调用 Redis XADD。"""
        from agent.task_queue import enqueue_task, _get_redis, TASK_STREAM

        mock_redis = MagicMock()
        mock_redis.xadd = AsyncMock()
        mock_redis.delete = AsyncMock()
        mock_redis.set = AsyncMock()

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            await enqueue_task(
                messages=[{"type": "human", "content": "分析AI芯片"}],
                initial_search_query_count=3,
                max_research_loops=5,
                reasoning_model="qwen-test",
            )

        # 验证 xadd 被调用
        mock_redis.xadd.assert_called_once()
        call_args = mock_redis.xadd.call_args
        # xadd(stream_name, {"task": payload}, maxlen=...)
        # call_args = ((stream_name, {"task": payload}), {"maxlen": ...})
        assert call_args[0][0] == TASK_STREAM
        payload = json.loads(call_args[0][1]["task"])
        assert payload["initial_search_query_count"] == 3
        assert payload["max_research_loops"] == 5
        assert payload["reasoning_model"] == "qwen-test"
        assert "task_id" in payload

    @pytest.mark.asyncio
    async def test_reused_task_id_starts_with_clean_event_stream(self):
        from agent.task_queue import enqueue_task, EVENT_STREAM_PREFIX, CANCEL_KEY_PREFIX

        mock_redis = MagicMock()
        mock_redis.xadd = AsyncMock()
        mock_redis.delete = AsyncMock()
        mock_redis.set = AsyncMock()

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            task_id = await enqueue_task(
                messages=[],
                initial_search_query_count=1,
                max_research_loops=1,
                reasoning_model="qwen-test",
                task_id="existing-task",
            )

        assert task_id == "existing-task"
        mock_redis.delete.assert_any_await(
            f"{EVENT_STREAM_PREFIX}:existing-task"
        )
        mock_redis.delete.assert_any_await(
            f"{CANCEL_KEY_PREFIX}:existing-task"
        )

    @pytest.mark.asyncio
    async def test_request_task_cancellation_sets_expiring_flag(self):
        from agent.task_queue import request_task_cancellation, CANCEL_KEY_PREFIX

        mock_redis = MagicMock()
        mock_redis.set = AsyncMock()
        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            await request_task_cancellation("task-1")

        mock_redis.set.assert_awaited_once_with(
            f"{CANCEL_KEY_PREFIX}:task-1", "1", ex=3600
        )


# ═══════════════════════════════════════════════════════════════════════
# read_task_events 测试
# ═══════════════════════════════════════════════════════════════════════

class TestReadTaskEvents:
    @pytest.mark.asyncio
    async def test_read_events_yields_from_stream(self):
        """验证从 Stream 读取事件并 yield。"""
        from agent.task_queue import read_task_events, _get_redis

        mock_redis = MagicMock()
        # 模拟一次 xread 返回一个事件
        mock_redis.xread = AsyncMock(return_value=[
            [
                b"research:events:test-id",
                [
                    ("1234567890-0", {"event": json.dumps({"generate_plan": {"plan": "test plan"}})}),
                ],
            ]
        ])

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            gen = read_task_events("test-id")
            results = []
            async for event_str in gen:
                results.append(event_str)
                break  # 只取第一个事件

        assert len(results) == 1
        event_id, event_str = results[0]
        assert event_id == "1234567890-0"
        event = json.loads(event_str)
        assert "generate_plan" in event

    @pytest.mark.asyncio
    async def test_error_event_stops_generator(self):
        """验证 error 事件会终止 generator。"""
        from agent.task_queue import read_task_events, _get_redis

        mock_redis = MagicMock()
        mock_redis.xread = AsyncMock(return_value=[
            [
                b"research:events:test-id",
                [
                    ("1234567890-0", {"event": json.dumps({"error": "测试错误"})}),
                ],
            ]
        ])

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            gen = read_task_events("test-id")
            results = []
            async for event_str in gen:
                results.append(event_str)

        assert len(results) == 1
        event = json.loads(results[0][1])
        assert event["error"] == "测试错误"

    @pytest.mark.asyncio
    async def test_finalize_answer_stops_generator(self):
        """验证 finalize_answer 事件会终止 generator。"""
        from agent.task_queue import read_task_events, _get_redis

        mock_redis = MagicMock()
        mock_redis.xread = AsyncMock(return_value=[
            [
                b"research:events:test-id",
                [
                    ("1234567890-0", {"event": json.dumps({"finalize_answer": {"messages": []}})}),
                ],
            ]
        ])

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            gen = read_task_events("test-id")
            results = []
            async for event_str in gen:
                results.append(event_str)

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_task_paused_stops_generator(self):
        """验证 task_paused 事件会终止 generator。"""
        from agent.task_queue import read_task_events, _get_redis

        mock_redis = MagicMock()
        mock_redis.xread = AsyncMock(return_value=[
            [
                b"research:events:test-id",
                [
                    ("1234567890-0", {"event": json.dumps({"task_paused": True})}),
                ],
            ]
        ])

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            gen = read_task_events("test-id")
            results = []
            async for event_str in gen:
                results.append(event_str)

        assert len(results) == 1

    @pytest.mark.asyncio
    async def test_task_cancelled_stops_generator(self):
        from agent.task_queue import read_task_events

        mock_redis = MagicMock()
        mock_redis.xread = AsyncMock(return_value=[
            [
                b"research:events:test-id",
                [("1234567890-0", {"event": json.dumps({"task_cancelled": True})})],
            ]
        ])

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            results = []
            async for event in read_task_events("test-id"):
                results.append(event)

        assert len(results) == 1
        assert json.loads(results[0][1])["task_cancelled"] is True


class TestWorkerReliability:
    def test_consumer_name_honors_explicit_configuration(self, monkeypatch):
        from agent.task_queue import build_consumer_name

        monkeypatch.setenv("TASK_WORKER_NAME", "worker-a")
        assert build_consumer_name() == "worker-a"

    @pytest.mark.asyncio
    async def test_read_task_reclaims_abandoned_delivery_first(self):
        from agent.task_queue import _read_task

        mock_redis = MagicMock()
        mock_redis.xautoclaim = AsyncMock(
            return_value=["0-0", [("1-0", {"task": "{}"})], []]
        )
        mock_redis.xreadgroup = AsyncMock()

        result = await _read_task(mock_redis, "worker-a")

        assert result == ("1-0", {"task": "{}"})
        mock_redis.xreadgroup.assert_not_awaited()

    @pytest.mark.asyncio
    async def test_failed_task_is_requeued_before_max_attempts(self, monkeypatch):
        import agent.task_queue as queue

        task = {
            "task_id": "task-1",
            "messages": [],
            "initial_search_query_count": 1,
            "max_research_loops": 1,
            "reasoning_model": "test-model",
            "attempt": 0,
        }
        mock_redis = MagicMock()
        for method in ("set", "xadd", "xack", "delete", "expire"):
            setattr(mock_redis, method, AsyncMock())
        mock_redis.exists = AsyncMock(return_value=0)

        class FailingGraph:
            async def astream(self, *args, **kwargs):
                if False:
                    yield None
                raise RuntimeError("temporary")

        monkeypatch.setattr(queue, "TASK_MAX_ATTEMPTS", 3)
        with (
            patch("agent.task_queue._read_task", return_value=(
                "1-0", {"task": json.dumps(task)}
            )),
            patch("agent.task_queue._get_graph", return_value=FailingGraph()),
        ):
            await queue._process_one_task(mock_redis, "worker-a")

        queued_payloads = [
            json.loads(call.args[1]["task"])
            for call in mock_redis.xadd.await_args_list
            if call.args[0] == queue.TASK_STREAM
        ]
        assert queued_payloads[-1]["attempt"] == 1
        mock_redis.xack.assert_awaited_once()

    @pytest.mark.asyncio
    async def test_final_failure_moves_task_to_dead_letter(self, monkeypatch):
        import agent.task_queue as queue

        task = {
            "task_id": "task-2",
            "messages": [],
            "initial_search_query_count": 1,
            "max_research_loops": 1,
            "reasoning_model": "test-model",
            "attempt": 2,
        }
        mock_redis = MagicMock()
        for method in ("set", "xadd", "xack", "delete", "expire"):
            setattr(mock_redis, method, AsyncMock())
        mock_redis.exists = AsyncMock(return_value=0)

        class FailingGraph:
            async def astream(self, *args, **kwargs):
                if False:
                    yield None
                raise RuntimeError("permanent")

        monkeypatch.setattr(queue, "TASK_MAX_ATTEMPTS", 3)
        with (
            patch("agent.task_queue._read_task", return_value=(
                "2-0", {"task": json.dumps(task)}
            )),
            patch("agent.task_queue._get_graph", return_value=FailingGraph()),
        ):
            await queue._process_one_task(mock_redis, "worker-b")

        streams = [call.args[0] for call in mock_redis.xadd.await_args_list]
        assert queue.DEAD_LETTER_STREAM in streams
        assert queue.TASK_STREAM not in streams

    def test_dedupe_sources_preserves_first_seen_order(self):
        from agent.task_queue import _dedupe_sources

        sources = [
            {"short_url": "s1", "value": "https://example.com/a", "label": "A"},
            {"short_url": "s2", "value": "https://example.com/b", "label": "B"},
            {"short_url": "s1", "value": "https://example.com/a", "label": "A"},
            {"short_url": "s3", "value": "https://example.com/a", "label": "A2"},
        ]

        assert _dedupe_sources(sources) == [
            {"short_url": "s1", "value": "https://example.com/a", "label": "A"},
            {"short_url": "s2", "value": "https://example.com/b", "label": "B"},
        ]

    @pytest.mark.asyncio
    async def test_non_terminal_events_continue(self):
        """验证非终止事件不会停止 generator（但测试中我们手动 break）。"""
        from agent.task_queue import read_task_events, _get_redis

        call_count = [0]
        mock_redis = MagicMock()

        async def mock_xread(*args, **kwargs):
            call_count[0] += 1
            if call_count[0] == 1:
                return [
                    [
                        b"research:events:test-id",
                        [("1234567890-0", {"event": json.dumps({"reflection": {}})})],
                    ]
                ]
            elif call_count[0] == 2:
                # 第二次调用时返回终止事件
                return [
                    [
                        b"research:events:test-id",
                        [("1234567890-1", {"event": json.dumps({"finalize_answer": {}})})],
                    ]
                ]
            return None

        mock_redis.xread = mock_xread

        with patch("agent.task_queue._get_redis", return_value=mock_redis):
            gen = read_task_events("test-id")
            results = []
            async for event_str in gen:
                results.append(event_str)

        # 应该收到两个事件：reflection + finalize_answer
        assert len(results) == 2
