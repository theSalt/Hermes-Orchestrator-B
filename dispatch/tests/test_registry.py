"""registry.py：SQLite 用户注册表（临时库）。"""

import pytest

from app.registry import Registry


@pytest.fixture()
async def reg(tmp_path):
    r = Registry(str(tmp_path / "registry.db"))
    await r.start()
    yield r
    await r.close()


async def test_create_and_get(reg):
    u = await reg.create_user("alice", "Alice")
    assert u.user_id == "alice"
    assert u.last_active is None
    got = await reg.get_user("alice")
    assert got is not None and got.display_name == "Alice"
    assert await reg.get_user("bob") is None


async def test_duplicate_user(reg):
    await reg.create_user("alice")
    with pytest.raises(KeyError):
        await reg.create_user("alice")


async def test_list_delete(reg):
    await reg.create_user("alice")
    await reg.create_user("bob")
    names = {u.user_id for u in await reg.list_users()}
    assert names == {"alice", "bob"}
    assert await reg.delete_user("alice") is True
    assert await reg.delete_user("alice") is False
    names = {u.user_id for u in await reg.list_users()}
    assert names == {"bob"}


async def test_touch_last_active(reg):
    await reg.create_user("alice")
    assert await reg.get_last_active("alice") is None
    await reg.touch("alice", 100.0)
    assert await reg.get_last_active("alice") == 100.0
    await reg.touch("alice", 200.0)
    assert await reg.get_last_active("alice") == 200.0


async def test_persistence(tmp_path):
    db = str(tmp_path / "registry.db")
    r1 = Registry(db)
    await r1.start()
    await r1.create_user("alice")
    await r1.touch("alice", 42.0)
    await r1.close()

    r2 = Registry(db)
    await r2.start()
    u = await r2.get_user("alice")
    assert u is not None
    assert await r2.get_last_active("alice") == 42.0
    await r2.close()
