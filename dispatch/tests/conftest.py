"""共享 fixtures：FakeRegistry（内存版注册表）与三处绑定注入。

routes.py 顶部绑定 `from .registry import registry`、main.py 同为顶部绑定，
manager.py 是函数内迟到 import（解析到 app.registry.registry）——因此 fake
必须 patch 三处，且要在 TestClient 触发 lifespan 之前完成。

环境变量必须在 conftest 顶部（先于任何 app 导入）设置：conftest 是 pytest
最先导入的模块，settings 单例在首次导入 app.config 时读取环境一次成型。
"""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="hermes-dispatch-test-")
os.environ.setdefault("HERMES_DATA_DIR", _tmp)
os.environ.setdefault("HERMES_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("HERMES_SECRET_KEY", "test-secret")

import time

import pytest

import app.main as main_mod
import app.registry as reg_mod
import app.routes as routes_mod
from app.registry import RegistryBackend, User


class FakeRegistry:
    """与 PgRegistry 相同 async 接口的内存实现（无持久化，仅测试用）。"""

    def __init__(self) -> None:
        self._users: dict[str, User] = {}

    async def start(self) -> None:
        pass

    async def close(self) -> None:
        pass

    async def create_user(self, user_id: str, display_name: str = "") -> User:
        if user_id in self._users:
            raise KeyError(f"user '{user_id}' already exists")
        u = User(
            user_id=user_id, display_name=display_name, created_at=time.time(), last_active=None
        )
        self._users[user_id] = u
        return u

    async def get_user(self, user_id: str) -> User | None:
        return self._users.get(user_id)

    async def list_users(self) -> list[User]:
        return sorted(self._users.values(), key=lambda u: (u.created_at, u.user_id))

    async def delete_user(self, user_id: str) -> bool:
        return self._users.pop(user_id, None) is not None

    async def touch(self, user_id: str, ts: float | None = None) -> None:
        u = self._users.get(user_id)
        if u is not None:
            u.last_active = ts if ts is not None else time.time()

    async def get_last_active(self, user_id: str) -> float | None:
        u = self._users.get(user_id)
        return u.last_active if u else None

    async def rotate_token_version(self, user_id: str) -> int | None:
        u = self._users.get(user_id)
        if u is None:
            return None
        u.token_version += 1
        return u.token_version


def assert_matches_backend(reg) -> None:
    """防 fake/实现漂移：两套实现都必须满足同一 Protocol。"""
    assert isinstance(reg, RegistryBackend)


@pytest.fixture()
def fake_reg(monkeypatch):
    reg = FakeRegistry()
    assert_matches_backend(reg)
    monkeypatch.setattr(reg_mod, "registry", reg)
    monkeypatch.setattr(main_mod, "registry", reg)
    monkeypatch.setattr(routes_mod, "registry", reg)
    return reg
