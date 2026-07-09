# mypy: disable - error - code = "no-untyped-def,misc"
import json
import asyncio
import hmac
import os
import re
import time
import traceback
import uuid
from contextlib import asynccontextmanager
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest
from loguru import logger
from agent.logger import setup_logger, log_request_details
from agent.configuration import get_default_model_id, load_available_models_from_env
from agent.task_queue import (
    enqueue_task,
    start_worker,
    read_task_events,
    request_task_cancellation,
    get_task_status,
)
from agent.auth.middleware import AuthMiddleware
from agent.auth.routes import router as auth_router
from agent.auth.authorization import user_owns_thread
from agent.audit import write_audit_event
from agent.db.engine import get_session_factory
from agent.db.models import UserThread
from agent.db.results import get_research_result, list_user_research_history
from agent.runtime_config import validate_runtime_config
from agent.limits import admit_research, release_research_slot
from agent.observability import (
    HTTP_ACTIVE,
    HTTP_DURATION,
    HTTP_REQUESTS,
    TASK_QUEUE_DEPTH,
    TASK_QUEUE_PENDING,
    request_id_var,
)
from sqlalchemy import delete, select

# ── 应用 lifespan（管理后台任务启动/关闭）───────────────────────────


@asynccontextmanager
async def _app_lifespan(app: FastAPI):
    """应用级 lifespan：管理后台任务的启动和关闭."""
    validate_runtime_config("api")
    worker_task = None
    if os.getenv("EMBEDDED_TASK_WORKER", "false").lower() in {"1", "true", "yes"}:
        worker_task = asyncio.create_task(start_worker())
        logger.warning("[TaskQueue] 已启用内嵌 Worker，仅建议本地开发使用")
    yield
    if worker_task is not None:
        worker_task.cancel()
        try:
            await worker_task
        except asyncio.CancelledError:
            pass
    # 关闭数据库引擎
    try:
        from agent.db.engine import close_engine
        await close_engine()
    except Exception as exc:
        logger.warning(f"[数据库] 关闭引擎时出错 ({type(exc).__name__}): {exc}")


app = FastAPI(docs_url=None, redoc_url=None, lifespan=_app_lifespan)
setup_logger()

# ── 认证中间件（最先添加 = 最外层，拦截非登录请求）─────────────
app.add_middleware(AuthMiddleware)

# ── 注册认证路由 ───────────────────────────────────────────────────
app.include_router(auth_router)

# ── API 路由 ─────────────────────────────────────────────────────────

# 添加获取模型列表的API端点
@app.get("/api/models")
async def get_available_models():
    """获取可用的LLM模型列表"""
    try:
        # 直接从环境变量加载模型列表
        models = load_available_models_from_env()
        models_data = [
            {
                "model_id": model.model_id,
                "display_name": model.display_name,
                "icon": model.icon,
                "icon_color": model.icon_color
            }
            for model in models
        ]
        logger.info(f"返回模型列表: {models_data}")
        return JSONResponse(content={"models": models_data})
    except ValueError as e:
        # 配置解析错误（如 AVAILABLE_MODELS JSON 格式错误）
        logger.error(f"模型配置解析失败 (ValueError): {e}")
        return JSONResponse(
            content={"error": "模型配置格式错误，请检查 AVAILABLE_MODELS 环境变量", "details": str(e)},
            status_code=500
        )
    except Exception as e:
        # 未知异常 — 记录完整 traceback 用于排查
        logger.error(f"获取模型列表失败 ({type(e).__name__}): {e}")
        logger.error(traceback.format_exc())
        return JSONResponse(
            content={"error": "获取模型列表失败", "details": str(e)},
            status_code=500
        )

# 添加请求日志中间件
@app.middleware("http")
async def log_requests(request: Request, call_next):
    try:
        # 记录请求基本信息
        logger.info(f"收到用户请求：{request.method} {request.url}")

        # 如果是POST请求且有body，记录详细信息
        if request.method in ["POST", "PUT", "PATCH"]:
            body = await request.body()
            if body:
                try:
                    body_data = json.loads(body.decode())
                    log_request_details(body_data)
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    logger.debug(
                        f"无法解析请求体为JSON ({type(e).__name__}): "
                        f"{body[:200]!r}"
                    )
                    log_request_details(body.decode())
    except Exception as e:
        # 日志记录本身的错误不应影响请求处理
        logger.error(
            f"记录请求日志时出错 ({type(e).__name__}): {e}\n"
            f"{traceback.format_exc()}"
        )

    try:
        response = await call_next(request)
        return response
    except Exception as e:
        logger.error(
            f"处理请求时出错 ({type(e).__name__}): {e}\n"
            f"请求: {request.method} {request.url}\n"
            f"{traceback.format_exc()}"
        )
        raise


@app.middleware("http")
async def observe_requests(request: Request, call_next):
    """Attach a correlation ID and collect bounded-cardinality HTTP metrics."""
    supplied = request.headers.get("X-Request-ID", "")
    request_id = (
        supplied
        if re.fullmatch(r"[A-Za-z0-9._-]{8,128}", supplied)
        else str(uuid.uuid4())
    )
    context_token = request_id_var.set(request_id)
    started_at = time.monotonic()
    HTTP_ACTIVE.inc()
    status_code = 500
    try:
        response = await call_next(request)
        status_code = response.status_code
        response.headers["X-Request-ID"] = request_id
        return response
    finally:
        route = request.scope.get("route")
        route_path = getattr(route, "path", "unmatched")
        HTTP_REQUESTS.labels(request.method, route_path, str(status_code)).inc()
        HTTP_DURATION.labels(request.method, route_path).observe(
            time.monotonic() - started_at
        )
        HTTP_ACTIVE.dec()
        request_id_var.reset(context_token)


@app.get("/health/live")
async def health_live():
    """Process liveness probe without downstream dependency checks."""
    return JSONResponse(content={"status": "ok"})


@app.get("/health/ready")
async def health_ready():
    """Readiness probe for Redis and PostgreSQL."""
    from sqlalchemy import text
    from agent.task_queue import _get_redis

    checks = {"redis": False, "postgres": False}
    try:
        checks["redis"] = bool(await (await _get_redis()).ping())
    except Exception:
        pass
    try:
        async with get_session_factory()() as session:
            checks["postgres"] = (await session.execute(text("SELECT 1"))).scalar() == 1
    except Exception:
        pass
    ready = all(checks.values())
    return JSONResponse(
        content={"status": "ok" if ready else "not_ready", "checks": checks},
        status_code=200 if ready else 503,
    )


@app.get("/metrics")
async def metrics(request: Request):
    """Prometheus endpoint protected by a dedicated bearer token."""
    expected = os.getenv("METRICS_TOKEN", "")
    provided = request.headers.get("Authorization", "")
    if not expected:
        return JSONResponse(content={"error": "指标端点未配置"}, status_code=503)
    if not hmac.compare_digest(provided, f"Bearer {expected}"):
        return JSONResponse(content={"error": "未授权"}, status_code=401)
    try:
        from agent.task_queue import CONSUMER_GROUP, TASK_STREAM, _get_redis

        redis_client = await _get_redis()
        TASK_QUEUE_DEPTH.set(await redis_client.xlen(TASK_STREAM))
        pending = await redis_client.xpending(TASK_STREAM, CONSUMER_GROUP)
        TASK_QUEUE_PENDING.set(int(pending.get("pending", 0)))
    except Exception as exc:
        logger.warning(f"[Metrics] 无法采集队列指标: {exc}")
    return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)


# ── 异步研究端点（任务队列 + SSE）────────────────────────────────────

@app.post("/api/research")
async def submit_research(request: Request):
    """提交研究任务，立即返回 task_id 和 SSE 流地址.

    请求体示例：
    {
        "messages": [{"type": "human", "content": "分析AI芯片市场趋势"}],
        "initial_search_query_count": 3,
        "max_research_loops": 3,
        "reasoning_model": "qwen-plus-latest"
    }
    """
    user_id: int = getattr(request.state, "user_id", 0)

    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"error": "请求体必须是 JSON 格式"},
            status_code=400,
        )

    requested_task_id = str(body.get("task_id", "")).strip()
    task_id = requested_task_id or str(uuid.uuid4())
    created_ownership = False

    # 恢复任务前必须先校验所有权，避免攻击者复用其他用户 task_id 清空事件流。
    if requested_task_id and not await user_owns_thread(user_id, task_id):
        await write_audit_event(
            "research_submit",
            "research_thread",
            "denied",
            user_id=user_id,
            resource_id=task_id,
            details={"reason": "foreign_or_missing_task"},
        )
        return JSONResponse(content={"error": "任务不存在"}, status_code=404)

    messages = body.get("messages", [])
    if not isinstance(messages, list):
        return JSONResponse(content={"error": "messages 必须是数组"}, status_code=400)

    try:
        query_count = int(body.get("initial_search_query_count", 2))
        loop_count = int(body.get("max_research_loops", 2))
    except (TypeError, ValueError):
        return JSONResponse(content={"error": "搜索次数参数必须是整数"}, status_code=400)
    max_queries = int(os.getenv("MAX_INITIAL_SEARCH_QUERIES", "5"))
    max_loops = int(os.getenv("MAX_RESEARCH_LOOPS_ALLOWED", "5"))
    if not 1 <= query_count <= max_queries or not 1 <= loop_count <= max_loops:
        return JSONResponse(
            content={
                "error": f"参数超限：初始查询 1-{max_queries}，研究循环 1-{max_loops}"
            },
            status_code=400,
        )

    try:
        admission = await admit_research(user_id)
    except Exception as exc:
        logger.exception(f"[Quota] 配额服务异常: {type(exc).__name__}: {exc}")
        return JSONResponse(content={"error": "配额服务暂时不可用"}, status_code=503)
    if not admission.allowed:
        return JSONResponse(
            content={"error": "请求过于频繁或并发任务已达上限", "reason": admission.reason},
            status_code=429,
            headers={"Retry-After": str(admission.retry_after)},
        )

    title_text = ""
    for message in messages:
        if (
            isinstance(message, dict)
            and message.get("type") == "human"
            and str(message.get("content", "")).strip()
        ):
            title_text = str(message["content"]).strip()[:256]
            break

    # 新任务先落所有权记录；若入队失败则补偿删除，避免不可访问的孤儿任务。
    try:
        if not requested_task_id:
            async with get_session_factory()() as session:
                session.add(UserThread(
                    user_id=user_id,
                    thread_id=task_id,
                    title=title_text,
                ))
                await session.commit()
                created_ownership = True
    except Exception as exc:
        await release_research_slot(user_id)
        logger.exception(f"[授权] 创建任务所有权失败 ({type(exc).__name__}): {exc}")
        await write_audit_event(
            "research_submit",
            "research_thread",
            "failed",
            user_id=user_id,
            resource_id=task_id,
            details={"reason": "ownership_create_failed"},
        )
        return JSONResponse(content={"error": "任务创建失败"}, status_code=503)

    try:
        reasoning_model = str(body.get("reasoning_model", "")).strip()
        if not reasoning_model:
            reasoning_model = get_default_model_id()
        await enqueue_task(
            messages=messages,
            initial_search_query_count=query_count,
            max_research_loops=loop_count,
            reasoning_model=reasoning_model,
            plan_status=body.get("plan_status", "unconfirmed"),
            plan=body.get("plan", ""),
            task_id=task_id,
            user_id=user_id,
        )
    except Exception as exc:
        if created_ownership:
            try:
                async with get_session_factory()() as session:
                    await session.execute(
                        delete(UserThread).where(
                            UserThread.user_id == user_id,
                            UserThread.thread_id == task_id,
                        )
                    )
                    await session.commit()
            except Exception as cleanup_exc:
                logger.exception(
                    f"[授权] 清理孤儿任务失败 task_id={task_id}: {cleanup_exc}"
                )
        logger.error(f"[TaskQueue] 任务入队失败 ({type(exc).__name__}): {exc}")
        await release_research_slot(user_id)
        await write_audit_event(
            "research_submit",
            "research_thread",
            "failed",
            user_id=user_id,
            resource_id=task_id,
            details={"reason": "enqueue_failed"},
        )
        return JSONResponse(content={"error": "任务提交失败"}, status_code=503)

    await write_audit_event(
        "research_submit",
        "research_thread",
        "success",
        user_id=user_id,
        resource_id=task_id,
        details={
            "resume": bool(requested_task_id),
            "initial_search_query_count": query_count,
            "max_research_loops": loop_count,
        },
    )
    return JSONResponse(content={
        "task_id": task_id,
        "stream_url": f"/api/research/{task_id}/stream",
    })


@app.get("/api/research/{task_id}/stream")
async def stream_research(task_id: str, request: Request):
    """SSE 端点：推送研究进度事件。

    客户端断开后可用 Last-Event-ID header 重连，不会丢失中间事件。
    """
    user_id: int = getattr(request.state, "user_id", 0)
    if not await user_owns_thread(user_id, task_id):
        return JSONResponse(content={"error": "任务不存在"}, status_code=404)

    last_event_id = request.headers.get("Last-Event-ID", "0")

    async def event_generator():
        try:
            async for event_id, event_str in read_task_events(task_id, last_event_id):
                yield f"id: {event_id}\ndata: {event_str}\n\n"
        except Exception as exc:
            logger.warning(f"[TaskQueue] SSE 推送异常 ({type(exc).__name__}): {exc}")
            yield f"data: {{\"error\": \"{exc}\"}}\n\n"

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",  # 禁用 nginx 缓冲
        },
    )


@app.post("/api/research/{task_id}/cancel")
async def cancel_research(task_id: str, request: Request):
    """请求协作式取消排队中或运行中的研究任务。"""
    user_id: int = getattr(request.state, "user_id", 0)
    if not await user_owns_thread(user_id, task_id):
        await write_audit_event(
            "research_cancel",
            "research_thread",
            "denied",
            user_id=user_id,
            resource_id=task_id,
            details={"reason": "foreign_or_missing_task"},
        )
        return JSONResponse(content={"error": "任务不存在"}, status_code=404)

    await request_task_cancellation(task_id)
    await write_audit_event(
        "research_cancel",
        "research_thread",
        "success",
        user_id=user_id,
        resource_id=task_id,
    )
    return JSONResponse(content={"task_id": task_id, "cancel_requested": True})


@app.get("/api/research/history")
async def research_history(request: Request, limit: int = 20):
    """Return the current user's recent research threads for UI restoration."""
    user_id: int = getattr(request.state, "user_id", 0)
    items = await list_user_research_history(user_id, limit=limit)
    await write_audit_event(
        "research_history_read",
        "research_thread",
        "success",
        user_id=user_id,
        details={"limit": max(1, min(limit, 50)), "count": len(items)},
    )
    return JSONResponse(content={"items": items})


@app.get("/api/research/{task_id}/status")
async def research_status(task_id: str, request: Request):
    """Return task lifecycle status after enforcing task ownership."""
    user_id: int = getattr(request.state, "user_id", 0)
    if not await user_owns_thread(user_id, task_id):
        return JSONResponse(content={"error": "任务不存在"}, status_code=404)
    status = await get_task_status(task_id)
    if status is None:
        return JSONResponse(content={"error": "任务状态不存在"}, status_code=404)
    return JSONResponse(content=status)


@app.get("/api/research/{task_id}/result")
async def research_result(task_id: str, request: Request):
    """Return a durable result after enforcing thread ownership."""
    user_id: int = getattr(request.state, "user_id", 0)
    if not await user_owns_thread(user_id, task_id):
        await write_audit_event(
            "research_result_read",
            "research_thread",
            "denied",
            user_id=user_id,
            resource_id=task_id,
            details={"reason": "foreign_or_missing_task"},
        )
        return JSONResponse(content={"error": "任务不存在"}, status_code=404)
    result = await get_research_result(task_id)
    if result is None:
        await write_audit_event(
            "research_result_read",
            "research_thread",
            "not_found",
            user_id=user_id,
            resource_id=task_id,
        )
        return JSONResponse(content={"error": "任务结果不存在"}, status_code=404)
    await write_audit_event(
        "research_result_read",
        "research_thread",
        "success",
        user_id=user_id,
        resource_id=task_id,
    )
    return JSONResponse(content={
        "task_id": task_id,
        "status": result.status,
        "report": result.report,
        "sources": result.sources or [],
        "error_type": result.error_type,
        "updated_at": result.updated_at.isoformat() if result.updated_at else None,
    })
