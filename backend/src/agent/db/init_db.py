"""初始化数据库：建表 + 写入初始用户.

用法：
    python -m agent.db.init_db           # 读取 .env 中的 DATABASE_URL
    python -m agent.db.init_db --drop    # 先删除已有表再重建
"""
from __future__ import annotations

import asyncio
import os
import sys

from dotenv import load_dotenv
import bcrypt as _bcrypt
from sqlalchemy import text

# 确保项目根目录在 sys.path 中
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
load_dotenv()

from agent.db.engine import get_engine
from agent.db.models import Base, User


async def _create_indexes() -> None:
    """创建必要的索引（独立于建表过程，确保一定生效）."""
    engine = get_engine()
    async with engine.begin() as conn:
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_users_username ON users(username)"
        ))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_threads_user_thread "
            "ON user_threads(user_id, thread_id)"
        ))
        await conn.execute(text(
            "CREATE UNIQUE INDEX IF NOT EXISTS idx_user_threads_thread "
            "ON user_threads(thread_id)"
        ))
        await conn.execute(text("""
            DO $$
            BEGIN
                IF NOT EXISTS (
                    SELECT 1 FROM pg_constraint
                    WHERE conname = 'fk_user_threads_user_id'
                ) THEN
                    ALTER TABLE user_threads
                    ADD CONSTRAINT fk_user_threads_user_id
                    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE;
                END IF;
            END $$;
        """))


def _hash_password(plain: str) -> str:
    """对明文密码进行 bcrypt 哈希."""
    return _bcrypt.hashpw(plain.encode("utf-8"), _bcrypt.gensalt()).decode("utf-8")


async def init_db(drop_first: bool = False) -> None:
    """创建所有表并写入初始用户."""
    engine = get_engine()

    if drop_first:
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.drop_all)
            print("[初始化] 已删除已有表")

    # 建表
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        print("[初始化] 表已创建")

    # 创建索引
    await _create_indexes()
    print("[初始化] 索引已创建")

    # 可选地创建一个显式配置的引导管理员；不再内置弱口令账号。
    from sqlalchemy import select
    from agent.db.engine import get_session_factory

    bootstrap_username = os.getenv("BOOTSTRAP_USERNAME", "").strip()
    bootstrap_password = os.getenv("BOOTSTRAP_PASSWORD", "")
    if not bootstrap_username and not bootstrap_password:
        print("[初始化] 未配置引导用户，跳过账号创建")
        print("[初始化] 数据库初始化完成")
        return
    if not bootstrap_username or not bootstrap_password:
        raise ValueError("BOOTSTRAP_USERNAME 和 BOOTSTRAP_PASSWORD 必须同时配置")
    if len(bootstrap_password) < 12:
        raise ValueError("BOOTSTRAP_PASSWORD 至少需要 12 个字符")

    async with get_session_factory()() as session:
        result = await session.execute(
            select(User).where(User.username == bootstrap_username)
        )
        existing = result.scalar_one_or_none()
        if existing is None:
            session.add(User(
                username=bootstrap_username,
                password=_hash_password(bootstrap_password),
            ))
            print(f"[初始化] 引导用户 {bootstrap_username} 已创建")
        else:
            print(f"[初始化] 引导用户 {bootstrap_username} 已存在，跳过")

        await session.commit()

    print("[初始化] 数据库初始化完成")


async def main() -> None:
    """命令行入口."""
    drop = "--drop" in sys.argv
    await init_db(drop_first=drop)


if __name__ == "__main__":
    asyncio.run(main())
