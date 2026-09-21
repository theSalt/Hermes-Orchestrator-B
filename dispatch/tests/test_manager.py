"""manager.ensure_ready 真实实现（driver 打桩，不碰 docker）。"""

import asyncio
import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="hermes-dispatch-test-")
os.environ.setdefault("HERMES_DATA_DIR", _tmp)
os.environ.setdefault("HERMES_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("HERMES_SECRET_KEY", "test-secret")

import pytest

from app import manager
from app.driver import AgentStatus
from app.security import agent_api_key, dispatch_token


async def test_ensure_ready_real_path(monkeypatch):
    calls = {"provision": 0}

    async def fake_status(uid):
        return AgentStatus(user_id=uid, state="absent")

    async def fake_provision(uid):
        calls["provision"] += 1
        return AgentStatus(user_id=uid, state="running", ip="10.1.1.1")

    async def fake_wait(uid, timeout):
        return "10.1.1.1"

    async def fake_config(uid):
        return False

    monkeypatch.setattr(manager.driver, "status", fake_status)
    monkeypatch.setattr(manager.driver, "provision", fake_provision)
    monkeypatch.setattr(manager.driver, "wait_healthy", fake_wait)
    monkeypatch.setattr(manager.driver, "ensure_agent_config", fake_config)

    ep = await manager.ensure_ready("alice")
    assert calls["provision"] == 1
    assert ep.base_url == "http://10.1.1.1:9120"
    assert ep.api_base_url == "http://10.1.1.1:8642"
    assert ep.api_key == agent_api_key("test-secret", "alice")
    assert ep.dispatch_tok == dispatch_token("test-secret", "alice")
    assert ep.started is True


async def test_ensure_ready_restart_on_unhealthy(monkeypatch):
    states = ["running", "running", "running", "running"]

    async def fake_status(uid):
        return AgentStatus(user_id=uid, state=states.pop(0), ip="10.1.1.1")

    async def fake_stop(uid):
        return None

    async def fake_provision(uid):
        return AgentStatus(user_id=uid, state="running", ip="10.1.1.2")

    # 第一次 wait 不健康 → 触发重启一轮；第二次健康
    waits = {"n": 0}

    async def fake_wait(uid, timeout):
        waits["n"] += 1
        return None if waits["n"] == 1 else "10.1.1.2"

    async def fake_config(uid):
        return False

    monkeypatch.setattr(manager.driver, "status", fake_status)
    monkeypatch.setattr(manager.driver, "stop", fake_stop)
    monkeypatch.setattr(manager.driver, "provision", fake_provision)
    monkeypatch.setattr(manager.driver, "wait_healthy", fake_wait)
    monkeypatch.setattr(manager.driver, "ensure_agent_config", fake_config)

    ep = await manager.ensure_ready("bob")
    assert waits["n"] == 2
    assert ep.base_url == "http://10.1.1.2:9120"


async def test_ensure_ready_short_wait_no_restart(monkeypatch):
    """探活的短等待：不健康时快速失败，不触发重启重试（交给后台预热）。"""
    async def fake_status(uid):
        return AgentStatus(user_id=uid, state="running", ip="10.1.1.1")

    async def fail_wait(uid, timeout):
        return None

    async def fake_provision(uid):
        raise AssertionError("短等待不应触发重启重试")

    monkeypatch.setattr(manager.driver, "status", fake_status)
    monkeypatch.setattr(manager.driver, "wait_healthy", fail_wait)
    monkeypatch.setattr(manager.driver, "stop", fake_provision)
    monkeypatch.setattr(manager.driver, "provision", fake_provision)

    with pytest.raises(RuntimeError):
        await manager.ensure_ready("alice", wait=7)


async def test_warm_async_runs_ensure_ready(monkeypatch):
    calls = []

    async def fake_ready(user_id, restart=False, wait=None):
        calls.append(user_id)

    monkeypatch.setattr(manager, "ensure_ready", fake_ready)
    manager.warm_async("alice")
    manager.warm_async("alice")  # 去重
    await asyncio.sleep(0.05)
    assert calls == ["alice"]


