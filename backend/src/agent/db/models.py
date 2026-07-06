"""数据库表模型（纯 SQL 表定义，不含 ORM 关系）."""
from __future__ import annotations

from sqlalchemy import Column, DateTime, ForeignKey, Integer, JSON, String, Text, UniqueConstraint, func
from sqlalchemy.orm import DeclarativeBase


class Base(DeclarativeBase):
    pass


class User(Base):
    """用户表."""
    __tablename__ = "users"

    id = Column(Integer, primary_key=True, autoincrement=True)
    username = Column(String(64), nullable=False, unique=True, index=True)
    password = Column(String(256), nullable=False)  # bcrypt 哈希
    created_at = Column(DateTime, server_default=func.now())


class UserThread(Base):
    """用户与研究线程的关联表（应用层管理 user_id → thread_id 映射）."""
    __tablename__ = "user_threads"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(
        Integer,
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    thread_id = Column(String(128), nullable=False, unique=True)
    title = Column(Text, nullable=True)  # 研究主题，用于前端展示历史记录
    created_at = Column(DateTime, server_default=func.now())

    __table_args__ = (
        UniqueConstraint("user_id", "thread_id", name="uq_user_threads_user_thread"),
        {"sqlite_autoincrement": True},
    )


class ResearchResult(Base):
    """Durable final result and lifecycle state for a research thread."""

    __tablename__ = "research_results"

    id = Column(Integer, primary_key=True, autoincrement=True)
    thread_id = Column(
        String(128),
        ForeignKey("user_threads.thread_id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    status = Column(String(32), nullable=False, index=True)
    report = Column(Text, nullable=True)
    sources = Column(JSON, nullable=False, default=list)
    error_type = Column(String(128), nullable=True)
    created_at = Column(DateTime, server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime,
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )


class AuditEvent(Base):
    """Append-only security and business audit event."""

    __tablename__ = "audit_events"

    id = Column(Integer, primary_key=True, autoincrement=True)
    user_id = Column(Integer, ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    action = Column(String(64), nullable=False, index=True)
    resource_type = Column(String(64), nullable=False)
    resource_id = Column(String(128), nullable=True)
    outcome = Column(String(32), nullable=False, index=True)
    request_id = Column(String(128), nullable=True, index=True)
    details = Column(JSON, nullable=False, default=dict)
    created_at = Column(DateTime, server_default=func.now(), nullable=False, index=True)
