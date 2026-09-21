"""用户注册表：SQLite（数据卷上，WAL 模式）。

多用户注册信息量小（用户/活跃度），SQLite 经 asyncio.to_thread 访问足够；
比起 A 方案的 PG 少一个外部依赖。容器重建不丢（数据卷持久化）。
"""

import asyncio
import logging
import os
import sqlite3
import time
from dataclasses import dataclass
from pathlib import Path

logger = logging.getLogger("hermes-dispatch.registry")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id      TEXT PRIMARY KEY,
    display_name TEXT NOT NULL DEFAULT '',
    created_at   REAL NOT NULL,
    last_active  REAL
);
"""


@dataclass
class User:
    user_id: str
    display_name: str
    created_at: float
    last_active: float | None

    def dict(self) -> dict:
        return {
            "user_id": self.user_id,
            "display_name": self.display_name,
            "created_at": self.created_at,
            "last_active": self.last_active,
        }


def _connect(db_path: str) -> sqlite3.Connection:
    Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    # asyncio.to_thread 每次可能在不同线程执行，允许跨线程复用连接；
    # 并发由 Registry._lock（asyncio 层）串行化
    conn = sqlite3.connect(db_path, timeout=10, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


class Registry:
    def __init__(self, db_path: str | None = None) -> None:
        self._db_path = db_path or os.path.join(
            os.environ.get("HERMES_DATA_DIR", "/data/DockerVolume/hermes-b"), "registry.db"
        )
        self._conn: sqlite3.Connection | None = None
        self._lock = asyncio.Lock()

    async def start(self) -> None:
        def _start() -> None:
            self._conn = _connect(self._db_path)
            self._conn.executescript(_SCHEMA)
            self._conn.commit()

        # asyncio.Lock 与事件循环绑定：每次 start 重建，
        # 兼容重启/测试中"同一单例跑在多个循环里"的情形
        self._lock = asyncio.Lock()
        async with self._lock:
            await asyncio.to_thread(_start)

    async def close(self) -> None:
        if self._conn is not None:
            await asyncio.to_thread(self._conn.close)
            self._conn = None

    def _require_conn(self) -> sqlite3.Connection:
        if self._conn is None:
            raise RuntimeError("registry not started")
        return self._conn

    async def create_user(self, user_id: str, display_name: str = "") -> User:
        def _create() -> User:
            conn = self._require_conn()
            now = time.time()
            conn.execute(
                "INSERT INTO users (user_id, display_name, created_at) VALUES (?, ?, ?)",
                (user_id, display_name, now),
            )
            conn.commit()
            return User(user_id, display_name, now, None)

        async with self._lock:
            try:
                return await asyncio.to_thread(_create)
            except sqlite3.IntegrityError as e:
                raise KeyError(f"user '{user_id}' already exists") from e

    async def get_user(self, user_id: str) -> User | None:
        def _get() -> User | None:
            row = self._require_conn().execute(
                "SELECT * FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return User(**dict(row)) if row else None

        async with self._lock:
            return await asyncio.to_thread(_get)

    async def list_users(self) -> list[User]:
        def _list() -> list[User]:
            rows = self._require_conn().execute(
                "SELECT * FROM users ORDER BY created_at"
            ).fetchall()
            return [User(**dict(r)) for r in rows]

        async with self._lock:
            return await asyncio.to_thread(_list)

    async def delete_user(self, user_id: str) -> bool:
        def _delete() -> bool:
            conn = self._require_conn()
            cur = conn.execute("DELETE FROM users WHERE user_id = ?", (user_id,))
            conn.commit()
            return cur.rowcount > 0

        async with self._lock:
            return await asyncio.to_thread(_delete)

    async def touch(self, user_id: str, ts: float | None = None) -> None:
        """活跃度落库。代理路径高频调用：单行 UPDATE（WAL 下开销可接受）。"""
        def _touch() -> None:
            conn = self._require_conn()
            conn.execute(
                "UPDATE users SET last_active = ? WHERE user_id = ?",
                (ts if ts is not None else time.time(), user_id),
            )
            conn.commit()

        async with self._lock:
            await asyncio.to_thread(_touch)

    async def get_last_active(self, user_id: str) -> float | None:
        def _get() -> float | None:
            row = self._require_conn().execute(
                "SELECT last_active FROM users WHERE user_id = ?", (user_id,)
            ).fetchone()
            return row["last_active"] if row and row["last_active"] else None

        async with self._lock:
            return await asyncio.to_thread(_get)


# 进程级单例（main lifespan 负责 start/close）
registry = Registry()
