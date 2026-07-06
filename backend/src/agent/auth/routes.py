"""登录路由."""
from __future__ import annotations

import os

from fastapi import APIRouter, Request, Response
from fastapi.responses import JSONResponse
from loguru import logger
from sqlalchemy import select
import bcrypt as _bcrypt

from agent.db.engine import get_session_factory
from agent.db.models import User
from agent.auth.session import (
    SESSION_ABSOLUTE_TTL,
    SESSION_COOKIE_NAME,
    create_session,
    delete_session,
    delete_user_sessions,
)
from agent.audit import write_audit_event

router = APIRouter()


@router.post("/api/login")
async def login(request: Request, response: Response):
    """用户登录接口.

    请求体：{"username": "zhangsan", "password": "zhangsan"}
    成功：设置 session_id Cookie，返回用户信息
    失败：返回 401
    """
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(
            content={"error": "请求体必须是 JSON 格式"},
            status_code=400,
        )

    username = body.get("username", "").strip()
    password = body.get("password", "")

    if not username or not password:
        return JSONResponse(
            content={"error": "用户名和密码不能为空"},
            status_code=400,
        )

    # 从数据库查找用户。基础设施异常必须返回 JSON，避免前端拿到纯文本 500。
    try:
        async with get_session_factory()() as session:
            result = await session.execute(
                select(User).where(User.username == username)
            )
            user = result.scalar_one_or_none()
    except Exception as exc:
        logger.exception(f"[Auth] 查询用户失败: {type(exc).__name__}: {exc}")
        return JSONResponse(
            content={"error": "用户数据库暂时不可用，请检查 PostgreSQL 服务"},
            status_code=503,
        )

    if user is None:
        logger.warning(f"[Auth] 登录失败：用户 {username} 不存在")
        await write_audit_event(
            "login",
            "session",
            "denied",
            details={"reason": "unknown_user"},
        )
        return JSONResponse(
            content={"error": "用户名或密码错误"},
            status_code=401,
        )

    # 验证密码
    try:
        password_ok = _bcrypt.checkpw(
            password.encode("utf-8"),
            user.password.encode("utf-8"),
        )
    except Exception:
        password_ok = False

    if not password_ok:
        logger.warning(f"[Auth] 登录失败：用户 {username} 密码错误")
        await write_audit_event(
            "login",
            "session",
            "denied",
            user_id=user.id,
            details={"reason": "bad_password"},
        )
        return JSONResponse(
            content={"error": "用户名或密码错误"},
            status_code=401,
        )

    # 创建 Redis 会话
    try:
        session_id = await create_session(user.id, user.username)
    except Exception as exc:
        logger.exception(f"[Auth] 创建会话失败: {type(exc).__name__}: {exc}")
        return JSONResponse(
            content={"error": "会话服务暂时不可用，请检查 Redis 服务"},
            status_code=503,
        )

    logger.info(f"[Auth] 用户 {username} 登录成功")
    await write_audit_event("login", "session", "success", user_id=user.id)

    resp = JSONResponse(content={
        "username": user.username,
        "user_id": user.id,
    })
    # 设置 Cookie（HttpOnly 防 XSS，SameSite Lax 防 CSRF）
    resp.set_cookie(
        key=SESSION_COOKIE_NAME,
        value=session_id,
        httponly=True,
        samesite="lax",
        max_age=SESSION_ABSOLUTE_TTL,
        path="/",
        secure=os.getenv("SESSION_COOKIE_SECURE", "false").lower()
        in {"1", "true", "yes"},
    )
    return resp


@router.post("/api/logout")
async def logout(request: Request, response: Response):
    """用户登出接口：删除 Redis 会话并清除 Cookie."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if session_id:
        await delete_session(session_id)

    resp = JSONResponse(content={"message": "已登出"})
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp


@router.get("/api/whoami")
async def whoami(request: Request):
    """获取当前登录用户信息（用于前端判断登录状态）."""
    session_id = request.cookies.get(SESSION_COOKIE_NAME)
    if not session_id:
        return JSONResponse(content={"logged_in": False})

    from agent.auth.session import get_session
    session_data = await get_session(session_id)
    if session_data is None:
        return JSONResponse(content={"logged_in": False})

    return JSONResponse(content={
        "logged_in": True,
        "username": session_data["username"],
        "user_id": int(session_data["user_id"]),
    })


@router.post("/api/password")
async def change_password(request: Request):
    """Change the current user's password and invalidate all sessions."""
    try:
        body = await request.json()
    except Exception:
        return JSONResponse(content={"error": "请求体必须是 JSON 格式"}, status_code=400)
    current_password = str(body.get("current_password", ""))
    new_password = str(body.get("new_password", ""))
    if len(new_password) < 12:
        return JSONResponse(content={"error": "新密码至少需要 12 个字符"}, status_code=400)
    user_id = int(request.state.user_id)
    async with get_session_factory()() as session:
        result = await session.execute(select(User).where(User.id == user_id))
        user = result.scalar_one_or_none()
        if user is None or not _bcrypt.checkpw(
            current_password.encode("utf-8"), user.password.encode("utf-8")
        ):
            await write_audit_event(
                "password_change",
                "user",
                "denied",
                user_id=user_id,
                resource_id=str(user_id),
                details={"reason": "bad_current_password"},
            )
            return JSONResponse(content={"error": "当前密码错误"}, status_code=401)
        user.password = _bcrypt.hashpw(
            new_password.encode("utf-8"), _bcrypt.gensalt()
        ).decode("utf-8")
        await session.commit()
    await delete_user_sessions(user_id)
    await write_audit_event(
        "password_change",
        "user",
        "success",
        user_id=user_id,
        resource_id=str(user_id),
    )
    resp = JSONResponse(content={"message": "密码已更新，请重新登录"})
    resp.delete_cookie(SESSION_COOKIE_NAME, path="/")
    return resp
