"""用户注册表：PostgreSQL（asyncpg，真 async）。

注册信息量小（用户/活跃度/token 版本），连接池 min=1/max=5 足够。
并发不再靠应用层锁串行化，由单语句原子性 + 主键约束保证：
  - create_user 用 INSERT ... ON CONFLICT DO NOTHING RETURNING（重复 → KeyError → 409）
  - rotate_token_version 用 UPDATE ... RETURNING（无 check-then-act 窗口）
生产路径唯一实现 PgRegistry；离线单测用 tests/conftest.py 的 FakeRegistry
（同接口内存版），两者都满足 RegistryBackend Protocol。

历史说明：初版为数据卷上的 SQLite（registry.db）；换 PG 后不再读取，
旧文件留在卷上无副作用，可手动清理。
"""

import re
import time
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import asyncpg

from .config import settings

# 表名仅用于测试注入独立表；白名单校验防拼接注入
_TABLE_RE = re.compile(r"^[a-z_][a-z0-9_]*$")

_COLUMNS = "user_id, display_name, created_at, last_active, token_version"


@dataclass
class User:
    user_id: str
    display_name: str
    created_at: float
    last_active: float | None
    # 0 = 初代派生（消息 dispatch:<uid>）；管理台轮换一次 +1（消息带 #v<n>）
    token_version: int = 0

    def dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_active": self.last_active,
            "token_version": self.token_version,
        }


@runtime_checkable
class RegistryBackend(Protocol):
    async def start(self) -> None: ...

    async def close(self) -> None: ...

    async def create_user(self, user_id: str, display_name: str = "") -> User: ...

    async def get_user(self, user_id: str) -> User | None: ...

    async def list_users(self) -> list[User]: ...

    async def delete_user(self, user_id: str) -> bool: ...

    async def touch(self, user_id: str, ts: float | None = None) -> None: ...

    async def get_last_active(self, user_id: str) -> float | None: ...

    async def rotate_token_version(self, user_id: str) -> int | None: ...


def _user(row: asyncpg.Record) -> User:
    return User(**dict(row))


class PgRegistry:
    def __init__(self, dsn: str | None = None, table: str = "users") -> None:
        if not _TABLE_RE.fullmatch(table or ""):
            raise ValueError(f"invalid registry table name: {table!r}")
        self._dsn = dsn if dsn is not None else settings.database_url
        self._table = table
        self._pool: asyncpg.Pool | None = None

    async def start(self) -> None:
        if not self._dsn:
            raise RuntimeError(
                "HERMES_DATABASE_URL 未设置（compose 部署已默认组装；直跑需显式配置）"
            )
        self._pool = await asyncpg.create_pool(
            self._dsn, min_size=1, max_size=5, command_timeout=10
        )
        async with self._pool.acquire() as conn:
            await conn.execute(f"""
                CREATE TABLE IF NOT EXISTS {self._table} (
                    user_id       TEXT PRIMARY KEY,
                    display_name  TEXT NOT NULL DEFAULT '',
                    created_at    DOUBLE PRECISION NOT NULL,
                    last_active   DOUBLE PRECISION,
                    token_version INTEGER NOT NULL DEFAULT 0
                )""")

    async def close(self) -> None:
        if self._pool is not None:
            await self._pool.close()
            self._pool = None

    def _require_pool(self) -> asyncpg.Pool:
        if self._pool is None:
            raise RuntimeError("registry not started")
        return self._pool

    async def create_user(self, user_id: str, display_name: str = "") -> User:
        row = await self._require_pool().fetchrow(
            f"INSERT INTO {self._table} (user_id, display_name, created_at) "
            "VALUES ($1, $2, $3) "
            "ON CONFLICT (user_id) DO NOTHING "
            f"RETURNING {_COLUMNS}",
            user_id,
            display_name,
            time.time(),
        )
        if row is None:
            raise KeyError(f"user '{user_id}' already exists")
        return _user(row)

    async def get_user(self, user_id: str) -> User | None:
        row = await self._require_pool().fetchrow(
            f"SELECT {_COLUMNS} FROM {self._table} WHERE user_id = $1", user_id
        )
        return _user(row) if row else None

    async def list_users(self) -> list[User]:
        # 双键排序：同秒创建的次序确定性（与 FakeRegistry 语义对齐）
        rows = await self._require_pool().fetch(
            f"SELECT {_COLUMNS} FROM {self._table} ORDER BY created_at, user_id"
        )
        return [_user(r) for r in rows]

    async def delete_user(self, user_id: str) -> bool:
        tag = await self._require_pool().execute(
            f"DELETE FROM {self._table} WHERE user_id = $1", user_id
        )
        return tag == "DELETE 1"

    async def touch(self, user_id: str, ts: float | None = None) -> None:
        """活跃度落库。代理路径高频调用：单行 UPDATE。"""
        await self._require_pool().execute(
            f"UPDATE {self._table} SET last_active = $2 WHERE user_id = $1",
            user_id,
            ts if ts is not None else time.time(),
        )

    async def get_last_active(self, user_id: str) -> float | None:
        row = await self._require_pool().fetchrow(
            f"SELECT last_active FROM {self._table} WHERE user_id = $1", user_id
        )
        return row["last_active"] if row else None

    async def rotate_token_version(self, user_id: str) -> int | None:
        """token_version+1 并返回新值；用户不存在返回 None。"""
        row = await self._require_pool().fetchrow(
            f"UPDATE {self._table} SET token_version = token_version + 1 "
            "WHERE user_id = $1 "
            "RETURNING token_version",
            user_id,
        )
        return row["token_version"] if row else None


# 进程级单例（main lifespan 负责 start/close）
registry = PgRegistry()
