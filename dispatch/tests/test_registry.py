"""注册表语义（离线跑 FakeRegistry；真实 PG 语义见 test_registry_pg.py）。"""

import pytest

from app.registry import PgRegistry, User
from tests.conftest import FakeRegistry, assert_matches_backend


async def test_fake_registry_protocol_conformance():
    assert_matches_backend(FakeRegistry())


async def test_create_default_token_version_zero():
    reg = FakeRegistry()
    u1 = await reg.create_user("alice")
    assert u1.token_version == 0
    assert u1.display_name == ""


async def test_create_get_list_delete_roundtrip():
    reg = FakeRegistry()
    await reg.create_user("alice", "Alice")
    await reg.create_user("bob", "Bob")

    u = await reg.get_user("alice")
    assert u is not None and u.display_name == "Alice"

    ids = [u.user_id for u in await reg.list_users()]
    assert ids == ["alice", "bob"]  # created_at 平手时按 user_id 次级排序

    assert await reg.delete_user("alice") is True
    assert await reg.delete_user("alice") is False
    assert await reg.get_user("alice") is None


async def test_duplicate_create_raises_keyerror():
    reg = FakeRegistry()
    await reg.create_user("dup")
    with pytest.raises(KeyError):
        await reg.create_user("dup")


async def test_touch_and_last_active():
    reg = FakeRegistry()
    await reg.create_user("alice")
    assert await reg.get_last_active("alice") is None
    await reg.touch("alice", 123.0)
    assert await reg.get_last_active("alice") == 123.0
    await reg.touch("alice")  # 缺省取当前时间
    assert await reg.get_last_active("alice") is not None
    # 未注册用户 touch 是 no-op
    await reg.touch("nobody", 1.0)
    assert await reg.get_last_active("nobody") is None


async def test_rotate_token_version():
    reg = FakeRegistry()
    await reg.create_user("alice")
    v1 = await reg.rotate_token_version("alice")
    v2 = await reg.rotate_token_version("alice")
    assert (v1, v2) == (1, 2)
    assert (await reg.get_user("alice")).token_version == 2
    assert await reg.rotate_token_version("nobody") is None


def test_pg_registry_rejects_bad_table_name():
    import pytest as _pytest

    with _pytest.raises(ValueError):
        PgRegistry(table="users; DROP TABLE users")


def test_user_dict_contains_token_version():
    u = User(user_id="a", display_name="", created_at=1.0, last_active=None)
    assert u.dict()["token_version"] == 0
