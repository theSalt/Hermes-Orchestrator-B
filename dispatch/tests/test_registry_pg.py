"""PgRegistry 集成测试：需要真实 PostgreSQL。

默认跳过；本地/CI 起一个一次性 PG 后设 HERMES_TEST_DATABASE_URL 即可运行：

    docker run -d --name hd-pg-test -e POSTGRES_USER=hermes -e POSTGRES_PASSWORD=test \
        -e POSTGRES_DB=hermes -p 54329:5432 postgres:16-alpine
    HERMES_TEST_DATABASE_URL=postgresql://hermes:test@127.0.0.1:54329/hermes \
        python -m pytest tests/test_registry_pg.py -q
"""

import os
import uuid

import pytest

pytestmark = pytest.mark.skipif(
    not os.environ.get("HERMES_TEST_DATABASE_URL"),
    reason="HERMES_TEST_DATABASE_URL 未设置，跳过 PG 集成测试",
)

from app.registry import PgRegistry
from tests.conftest import assert_matches_backend


@pytest.fixture()
async def reg():
    r = PgRegistry(
        dsn=os.environ["HERMES_TEST_DATABASE_URL"],
        table=f"users_test_{uuid.uuid4().hex[:8]}",
    )
    await r.start()
    yield r
    # 清理测试表（close 后无连接可用，需先 DROP 再 close）
    if r._pool is not None:
        async with r._pool.acquire() as conn:
            await conn.execute(f"DROP TABLE IF EXISTS {r._table}")
        await r.close()


async def test_pg_start_creates_schema_idempotent(reg):
    await reg.start()  # 二次 start 不报错
    row = await reg._pool.fetchrow(
        "SELECT 1 FROM information_schema.columns "
        "WHERE table_name = $1 AND column_name = 'token_version'",
        reg._table,
    )
    assert row is not None


async def test_pg_create_duplicate_raises(reg):
    await reg.create_user("alice")
    with pytest.raises(KeyError):
        await reg.create_user("alice")
    assert (await reg.get_user("alice")).token_version == 0


async def test_pg_rotate_returning(reg):
    await reg.create_user("alice")
    assert await reg.rotate_token_version("alice") == 1
    assert await reg.rotate_token_version("alice") == 2
    assert (await reg.get_user("alice")).token_version == 2
    assert await reg.rotate_token_version("nobody") is None


async def test_pg_touch_persistence(reg):
    await reg.create_user("alice")
    await reg.touch("alice", 42.5)
    # 跨实例（模拟进程重启）：新实例读同一张表
    other = PgRegistry(dsn=reg._dsn, table=reg._table)
    await other.start()
    try:
        u = await other.get_user("alice")
        assert u is not None and u.last_active == 42.5
        assert (await other.list_users())[0].user_id == "alice"
        assert_matches_backend(other)
    finally:
        await other.close()


async def test_pg_delete(reg):
    await reg.create_user("alice")
    assert await reg.delete_user("alice") is True
    assert await reg.delete_user("alice") is False
