"""基于 Redis Streams 的任务队列和事件流。

职责：
  - 任务入队：XADD research:tasks
  - 后台消费：XREADGROUP + 执行 LangGraph 图 + XACK
  - 事件发布：XADD research:events:{task_id}
  - SSE 读取：XREAD research:events:{task_id}

生产环境由独立 Worker 进程消费；本地开发可显式启用内嵌 Worker。
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import time
import uuid

import redis.asyncio as redis
from langchain_core.messages import HumanMessage, AIMessage
from loguru import logger

from agent.graph import build_graph
from agent.db.results import save_research_result
from agent.limits import release_research_slot
from agent.observability import TASK_DURATION, TASK_TRANSITIONS, task_id_var
from langgraph.checkpoint.memory import InMemorySaver
from langgraph.checkpoint.redis.aio import AsyncRedisSaver

REDIS_URL = os.getenv("REDIS_URL", "redis://127.0.0.1:6379/0")
TASK_STREAM = "research:tasks"
EVENT_STREAM_PREFIX = "research:events"
CONSUMER_GROUP = "research-workers"
DEAD_LETTER_STREAM = "research:tasks:dead-letter"
TASK_STATUS_PREFIX = "research:task-status"
# 单个任务的事件流最大保留条数。流式 token 数可能超过 500，允许通过
# 环境变量按报告长度调整，避免重连时丢失早期结构化事件。
EVENT_STREAM_MAXLEN = int(os.getenv("EVENT_STREAM_MAXLEN", "10000"))
# XREAD 阻塞超时（毫秒），避免空轮询
STREAM_BLOCK_MS = 5000
CANCEL_KEY_PREFIX = "research:cancel"
TASK_MAX_ATTEMPTS = max(1, int(os.getenv("TASK_MAX_ATTEMPTS", "3")))
TASK_CLAIM_IDLE_MS = max(1000, int(os.getenv("TASK_CLAIM_IDLE_MS", "60000")))
TASK_STATUS_TTL_SECONDS = max(3600, int(os.getenv("TASK_STATUS_TTL_SECONDS", "604800")))

# ── 子图内部节点名 → 前端事件名映射 ──────────────────────────────
# 重构为子图架构后，graph.astream() 需启用 subgraphs=True 才能获取子图内部事件。
# 子图内部节点名与前端约定的扁平事件名不一致，此处做翻译。
_SUBGRAPH_EVENT_MAP = {
    "generate_queries": "generate_query",
    "web_search": "web_research",
    "critique": "reflection",
}

_redis: redis.Redis | None = None
_persistent_graph = None  # 延迟初始化，带 Redis checkpoint 持久化


async def _get_redis() -> redis.Redis:
    """延迟获取 Redis 连接（复用同一个连接池）."""
    global _redis
    if _redis is None:
        _redis = redis.from_url(
            REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=3,
            # XREAD/XREADGROUP 最长阻塞 5 秒；读超时必须明显大于 block，
            # 否则空队列会被误判为 Redis 故障。
            socket_timeout=10,
        )
    return _redis


async def _get_graph():
    """获取编译图；Redis Stack 不可用时降级为进程内 checkpoint."""
    global _persistent_graph
    if _persistent_graph is None:
        try:
            checkpointer = AsyncRedisSaver(
                REDIS_URL,
                ttl={"default_ttl": 60 * 24 * 7, "refresh_on_read": True},
            )
            await checkpointer.asetup()
            _persistent_graph = build_graph(checkpointer=checkpointer)############!
            logger.info("[TaskQueue] 已创建 AsyncRedisSaver 持久化图（TTL=7天）")
        except redis.ResponseError as exc:
            # AsyncRedisSaver 依赖 RedisJSON + RediSearch。开发环境使用普通
            # redis:7-alpine 时没有 FT.* / JSON.* 命令，改用进程内 checkpoint，
            # 但 Redis Streams、Session 和搜索缓存仍正常使用 Redis。
            if "unknown command" not in str(exc).lower():
                raise##!
            logger.warning(
                "[TaskQueue] 当前 Redis 不含 RedisJSON/RediSearch，"
                "checkpoint 降级为 InMemorySaver（重启后状态不保留）"
            )
            _persistent_graph = build_graph(checkpointer=InMemorySaver())
    return _persistent_graph


# ═══════════════════════════════════════════════════════════════════════
# 任务入队
# ═══════════════════════════════════════════════════════════════════════

async def enqueue_task(
    messages: list[dict],
    initial_search_query_count: int,
    max_research_loops: int,
    reasoning_model: str,
    plan_status: str = "unconfirmed",
    plan: str = "",
    task_id: str = "",
    user_id: int = 0,
) -> str:
    """将研究任务写入 Redis Stream，返回 task_id。

    调用方（POST /api/research）拿到 task_id 后立即返回给前端。
    如果传入 task_id 则复用，否则生成新的 UUID。
    """
    r = await _get_redis()
    if not task_id:
        task_id = str(uuid.uuid4())
    else:
        # 同一 LangGraph thread 的下一阶段需要复用 task_id，但 SSE 属于
        # 新的一次运行。清除上一阶段的终止事件，避免新连接立刻读到
        # task_paused 后退出。
        await r.delete(f"{EVENT_STREAM_PREFIX}:{task_id}")
    await r.delete(f"{CANCEL_KEY_PREFIX}:{task_id}")
    task_payload = json.dumps({
        "task_id": task_id,
        "messages": messages,
        "initial_search_query_count": initial_search_query_count,
        "max_research_loops": max_research_loops,
        "reasoning_model": reasoning_model,
        "plan_status": plan_status,
        "plan": plan,
        "attempt": 0,
        "user_id": user_id,
    })
    await r.xadd(TASK_STREAM, {"task": task_payload}, maxlen=1000)
    await _set_task_status(r, task_id, "queued", attempt=0)
    logger.info(f"[TaskQueue] 任务已入队 task_id={task_id[:8]}...")
    return task_id


async def request_task_cancellation(task_id: str) -> None:
    """请求协作式取消排队中或运行中的研究任务。"""
    r = await _get_redis()
    await r.set(f"{CANCEL_KEY_PREFIX}:{task_id}", "1", ex=3600)


async def _is_task_cancelled(r: redis.Redis, task_id: str) -> bool:
    return bool(await r.exists(f"{CANCEL_KEY_PREFIX}:{task_id}"))


async def _set_task_status(
    r: redis.Redis,
    task_id: str,
    status: str,
    **details,
) -> None:
    """Persist a small, non-sensitive task lifecycle record."""
    payload = {"task_id": task_id, "status": status, **details}
    await r.set(
        f"{TASK_STATUS_PREFIX}:{task_id}",
        json.dumps(payload, ensure_ascii=False, default=str),
        ex=TASK_STATUS_TTL_SECONDS,
    )
    TASK_TRANSITIONS.labels(status).inc()


async def _persist_result_safely(task_id: str, status: str, **values) -> None:
    """Persist results without turning a completed LLM run into a costly retry."""
    try:
        if isinstance(values.get("sources"), list):
            values["sources"] = _dedupe_sources(values["sources"])
        await save_research_result(task_id, status, **values)
    except Exception as exc:
        logger.exception(
            f"[ResultStore] 持久化失败 task_id={task_id[:8]} status={status}: {exc}"
        )


def _dedupe_sources(sources: list[dict]) -> list[dict]:
    """Deduplicate source records while preserving first-seen order."""
    deduped: list[dict] = []
    seen: set[str] = set()
    for source in sources:
        if not isinstance(source, dict):
            continue
        canonical_url = str(source.get("value") or source.get("url") or "").strip()
        key = canonical_url or "|".join(
            part
            for part in (
                str(source.get("short_url") or "").strip(),
                str(source.get("label") or source.get("title") or "").strip(),
            )
            if part
        )
        if not key:
            continue
        if key in seen:
            continue
        seen.add(key)
        deduped.append(source)
    return deduped


async def get_task_status(task_id: str) -> dict | None:
    """Return the persisted lifecycle status for an existing task."""
    raw = await (await _get_redis()).get(f"{TASK_STATUS_PREFIX}:{task_id}")
    return json.loads(raw) if raw else None


# ═══════════════════════════════════════════════════════════════════════
# Worker 消费循环（独立进程或本地内嵌模式）
# ═══════════════════════════════════════════════════════════════════════

def build_consumer_name() -> str:
    """Build a unique Redis consumer name for every worker process."""
    configured = os.getenv("TASK_WORKER_NAME", "").strip()
    if configured:
        return configured
    return f"{socket.gethostname()}-{os.getpid()}-{uuid.uuid4().hex[:8]}"


async def start_worker(consumer_name: str | None = None) -> None:
    """启动后台消费协程，不阻塞 HTTP 请求处理。

    调用方式（在 app.py 的 startup 事件中）：
        asyncio.create_task(start_worker())
    """
    r = await _get_redis()
    await _ensure_consumer_group(r)
    consumer_name = consumer_name or build_consumer_name()
    logger.info(f"[TaskQueue] worker 已启动 consumer={consumer_name}，等待任务...")
    while True:
        try:
            await _process_one_task(r, consumer_name)
        except asyncio.CancelledError:
            logger.info("[TaskQueue] worker 协程被取消")
            break
        except Exception as exc:
            logger.error(f"[TaskQueue] worker 异常 ({type(exc).__name__}): {exc}")
            await asyncio.sleep(1)  # 短暂等待后继续


async def _ensure_consumer_group(r: redis.Redis) -> None:
    """创建 Consumer Group（如果不存在）."""
    try:
        await r.xgroup_create(TASK_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
        logger.info(f"[TaskQueue] Consumer Group '{CONSUMER_GROUP}' 已创建")
    except redis.ResponseError as e:
        if "BUSYGROUP" in str(e):
            logger.debug("[TaskQueue] Consumer Group 已存在，跳过创建")
        else:
            raise


def _build_initial_state(task: dict, messages: list) -> dict:
    """Build graph state from the queued task payload.

    ``reasoning_model`` must live in graph state because ResearchAgent and
    WriterAgent select their model from ``state["reasoning_model"]``. Keeping
    it only in RunnableConfig does not expose it to those nodes.
    """
    return {
        "messages": messages,
        "plan_status": task.get("plan_status", "unconfirmed"),
        "plan": task.get("plan", ""),
        "reasoning_model": task.get("reasoning_model", ""),
        "initial_search_query_count": int(
            task.get("initial_search_query_count", 2)
        ),
        "max_research_loops": int(task.get("max_research_loops", 2)),
        "research_loop_count": 0,
    }


async def _claim_abandoned_task(r: redis.Redis, consumer_name: str):
    """Claim one task left pending by a crashed worker after the idle timeout."""
    result = await r.xautoclaim(
        TASK_STREAM,
        CONSUMER_GROUP,
        consumer_name,
        TASK_CLAIM_IDLE_MS,
        "0-0",
        count=1,
    )
    if result and len(result) >= 2 and result[1]:
        return result[1][0]
    return None


async def _read_task(r: redis.Redis, consumer_name: str):
    """Read one abandoned or newly queued task for this consumer."""
    reclaimed = await _claim_abandoned_task(r, consumer_name)
    if reclaimed:
        logger.warning(f"[TaskQueue] 已接管遗留任务 redis_id={reclaimed[0]}")
        return reclaimed
    result = await r.xreadgroup(
        CONSUMER_GROUP,
        consumer_name,
        {TASK_STREAM: ">"},
        block=STREAM_BLOCK_MS,
        count=1,
    )
    if not result:
        return None
    return result[0][1][0]


async def _process_one_task(r: redis.Redis, consumer_name: str) -> None:
    """消费并执行一个任务."""
    queued_message = await _read_task(r, consumer_name)
    if not queued_message:
        return  # 超时，没有任务
    redis_msg_id, payload = queued_message
    task = json.loads(payload["task"])
    task_id = task["task_id"]
    user_id = int(task.get("user_id", 0))
    attempt = int(task.get("attempt", 0))
    started_at = time.monotonic()
    task_context_token = task_id_var.set(task_id)
    release_slot = False
    outcome = "error"

    logger.info(f"[TaskQueue] 开始执行任务 task_id={task_id[:8]}...")
    await _set_task_status(
        r, task_id, "running", attempt=attempt, consumer=consumer_name
    )

    try:
        # 重建消息列表（保留完整历史及原始 ID，确保 add_messages 按 ID 正确去重）
        messages = [
            HumanMessage(content=m["content"], id=m.get("id")) if m.get("type") == "human"
            else AIMessage(content=m["content"], id=m.get("id")) if m.get("type") == "ai"
            else None
            for m in task["messages"]
        ]
        messages = [m for m in messages if m is not None]

        # 这个函数将大模型流式输出的token借助Redis也流式输出到前端页面（供节点内 Agent 使用）
        async def emit_token(text: str, node: str) -> None:
            """将 LLM token 推送到 Redis 事件流."""
            try:
                event_json = json.dumps(
                    {"token": {"text": text, "node": node}},
                    ensure_ascii=False,
                )
                await r.xadd(
                    f"{EVENT_STREAM_PREFIX}:{task_id}",
                    {"event": event_json},
                    maxlen=EVENT_STREAM_MAXLEN,
                )
            except Exception as exc:
                logger.warning(f"[TaskQueue] token事件发送失败 node={node}: {exc}")

        config = {
            "configurable": {
                "thread_id": task_id,  # 将 task_id 作为 LangGraph 的 checkpoint thread_id
                "initial_search_query_count": task["initial_search_query_count"],
                "number_of_initial_queries": task["initial_search_query_count"],
                "max_research_loops": task["max_research_loops"],
                "reasoning_model": task["reasoning_model"],
                "_emit_token": emit_token,
            }
        }

        # 图初始状态（支持 plan 确认后的 resume 场景）
        initial_state = _build_initial_state(task, messages)

        # 执行图（Checkpointer可绑定 Redis，通过 thread_id 实现会话隔离）
        # subgraphs=True: 穿透子图边界，获取内部节点事件
        # 事件格式: (namespace, {node_name: node_output})
        # event = (("research:abc123",), {"generate_queries": {...}})
        #         └──── namespace ────┘  └──────── data ──────────┘
        #   - 父图节点:  namespace = (),     data = {"generate_plan": {...}}
        #   - 子图内部:  namespace = ("research:<id>",), data = {"generate_queries": {...}}
        final_answer_event = None
        gathered_sources: list[dict] = []
        cancelled = await _is_task_cancelled(r, task_id)
        persistent_graph = await _get_graph()
        async for event in persistent_graph.astream(initial_state, config, subgraphs=True):
            if await _is_task_cancelled(r, task_id):
                cancelled = True
                logger.info(f"[TaskQueue] 收到取消请求 task_id={task_id[:8]}...")
                break
            # ── 解析 subgraphs=True 的 2 元组格式 ─────────────────────
            #避免处理非节点事件，如：子图进入/退出时的边界事件、LangGraph调度层面的内部生命周期事件
            if not isinstance(event, tuple) or len(event) != 2:
                continue

            namespace: tuple = event[0]
            data: dict = event[1]

            if not isinstance(data, dict) or not data:
                continue

            # 从 data 中提取原始节点名和节点输出
            node_name = list(data.keys())[0]
            node_output = data[node_name]

            # 跳过 LangGraph 内部事件
            if not node_name or str(node_name).startswith("__"):
                continue

            # ── 事件名翻译：子图内部节点 → 前端扁平事件名 ────────────
            is_subgraph = len(namespace) > 0
            frontend_event_name = (
                _SUBGRAPH_EVENT_MAP.get(node_name, node_name)
                if is_subgraph
                else node_name
            )
            translated_event = {frontend_event_name: node_output}
            if isinstance(node_output, dict) and node_output.get("sources_gathered"):
                gathered_sources.extend(node_output["sources_gathered"])

            # 写入 Redis 事件流（前端根据事件名更新 UI，如气泡的显示，页面文本的合并等等或关闭SSE连接）
            event_json = json.dumps(translated_event, default=str, ensure_ascii=False)
            await r.xadd(
                f"{EVENT_STREAM_PREFIX}:{task_id}",
                {"event": event_json},
                maxlen=EVENT_STREAM_MAXLEN,
            )

            # 如果是 writer 子图返回（包含最终报告），提取为 finalize_answer
            if frontend_event_name == "write" and isinstance(node_output, dict):
                if "messages" in node_output:
                    # 子图返回的消息列表可能包含历史消息（由 add_messages reducer 累积），
                    # 取最后一条 AI 消息作为最终报告内容
                    msgs = node_output["messages"]
                    ai_msgs = [m for m in msgs if getattr(m, "type", None) == "ai"]
                    final_msg = ai_msgs[-1] if ai_msgs else (msgs[-1] if msgs else None)
                    if final_msg:
                        final_answer_event = {
                            "finalize_answer": {
                                "messages": [
                                    {"content": final_msg.content, "type": "ai"}
                                ]
                            }
                        }

        # 判断是正常完成还是暂停在 Plan 确认
        if cancelled:
            logger.info(f"[TaskQueue] 任务已取消 task_id={task_id[:8]}...")
            cancel_json = json.dumps({"task_cancelled": True}, ensure_ascii=False)
            await r.xadd(
                f"{EVENT_STREAM_PREFIX}:{task_id}",
                {"event": cancel_json},
                maxlen=EVENT_STREAM_MAXLEN,
            )
            await _set_task_status(r, task_id, "cancelled", attempt=attempt)
            await _persist_result_safely(task_id, "cancelled")
            release_slot = True
            outcome = "cancelled"
        elif final_answer_event:
            logger.info(f"[TaskQueue] 任务完成 task_id={task_id[:8]}...")
            # 图正常完成 → 发射 finalize_answer
            event_json = json.dumps(final_answer_event, default=str, ensure_ascii=False)
            await r.xadd(
                f"{EVENT_STREAM_PREFIX}:{task_id}",
                {"event": event_json},
                maxlen=EVENT_STREAM_MAXLEN,
            )
            await _set_task_status(r, task_id, "completed", attempt=attempt)
            report = final_answer_event["finalize_answer"]["messages"][0]["content"]
            await _persist_result_safely(
                task_id, "completed", report=report, sources=gathered_sources
            )
            release_slot = True
            outcome = "completed"
        else:
            logger.info(f"[TaskQueue] 任务已暂停 task_id={task_id[:8]}...")
            # 图执行完毕但未生成最终答案 → 在 Plan 确认处暂停
            pause_json = json.dumps({"task_paused": True}, ensure_ascii=False)
            await r.xadd(
                f"{EVENT_STREAM_PREFIX}:{task_id}",
                {"event": pause_json},
                maxlen=EVENT_STREAM_MAXLEN,
            )
            await _set_task_status(r, task_id, "paused", attempt=attempt)
            await _persist_result_safely(task_id, "paused", sources=gathered_sources)
            release_slot = True
            outcome = "paused"

    except Exception as exc:
        logger.error(f"[TaskQueue] 任务失败 task_id={task_id[:8]}... ({type(exc).__name__}): {exc}")
        next_attempt = attempt + 1
        if next_attempt < TASK_MAX_ATTEMPTS:
            task["attempt"] = next_attempt
            await r.xadd(
                TASK_STREAM,
                {"task": json.dumps(task, ensure_ascii=False)},
                maxlen=1000,
            )
            await _set_task_status(
                r,
                task_id,
                "retrying",
                attempt=next_attempt,
                error_type=type(exc).__name__,
            )
            await _persist_result_safely(
                task_id, "retrying", error_type=type(exc).__name__
            )
            logger.warning(
                f"[TaskQueue] 任务将重试 task_id={task_id[:8]}... "
                f"attempt={next_attempt}/{TASK_MAX_ATTEMPTS - 1}"
            )
            outcome = "retrying"
        else:
            dead_letter = {
                "task": task,
                "failed_redis_id": redis_msg_id,
                "attempts": next_attempt,
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            await r.xadd(
                DEAD_LETTER_STREAM,
                {"failure": json.dumps(dead_letter, ensure_ascii=False, default=str)},
                maxlen=1000,
            )
            error_json = json.dumps(
                {"error": "任务执行失败，已超过最大重试次数"},
                ensure_ascii=False,
            )
            await r.xadd(
                f"{EVENT_STREAM_PREFIX}:{task_id}",
                {"event": error_json},
                maxlen=EVENT_STREAM_MAXLEN,
            )
            await _set_task_status(
                r,
                task_id,
                "failed",
                attempt=next_attempt,
                error_type=type(exc).__name__,
            )
            await _persist_result_safely(
                task_id, "failed", error_type=type(exc).__name__
            )
            release_slot = True
            outcome = "failed"

    finally:
        # 确认消息已处理
        await r.xack(TASK_STREAM, CONSUMER_GROUP, redis_msg_id)
        await r.delete(f"{CANCEL_KEY_PREFIX}:{task_id}")
        # 清理事件流（设置过期，24 小时后自动删除）
        await r.expire(f"{EVENT_STREAM_PREFIX}:{task_id}", 86400)
        if release_slot:
            try:
                await release_research_slot(user_id)
            except Exception as exc:
                logger.error(f"[Quota] 释放用户并发槽失败 user_id={user_id}: {exc}")
        TASK_DURATION.labels(outcome).observe(time.monotonic() - started_at)
        task_id_var.reset(task_context_token)


# ═══════════════════════════════════════════════════════════════════════
# SSE 事件读取
# ═══════════════════════════════════════════════════════════════════════

async def read_task_events(task_id: str, last_event_id: str = "0"):
    """Generator: 从 Redis Stream 读取任务事件，用于 SSE 推送。

    支持断线重连：客户端通过 SSE 的 Last-Event-ID header 传入 last_event_id，
    服务端从该 ID 之后开始推送。
    """
    r = await _get_redis()
    stream_key = f"{EVENT_STREAM_PREFIX}:{task_id}"
    current_id = last_event_id

    while True:
        try:
            result = await r.xread(
                {stream_key: current_id},
                block=STREAM_BLOCK_MS,
                count=10,
            )
            if result:
                for _, messages in result:
                    for msg_id, data in messages:
                        event_str = data.get("event", "{}")
                        current_id = msg_id
                        yield msg_id, event_str

                        # 检查是否为终止事件（错误 / finalize_answer / task_paused）
                        try:
                            event = json.loads(event_str)
                            if any(
                                k in event
                                for k in ("error", "finalize_answer", "task_paused", "task_cancelled")
                            ):
                                return  # 任务结束，关闭 SSE 连接
                        except json.JSONDecodeError:
                            pass

        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning(f"[TaskQueue] SSE 读取异常 ({type(exc).__name__}): {exc}")
            await asyncio.sleep(1)
