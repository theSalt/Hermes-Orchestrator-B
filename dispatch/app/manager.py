"""按用户编排：确保容器就绪、活跃度维护、空闲回收。

改造自 A 方案 manager.py：policy 体系收敛为 env 全局配置（B 方案按用户数有限、
统一限额），其余生命周期机制原样保留。新增：活跃 WS 连接计数（>0 的用户不回收）。
"""

import asyncio
import logging
import time
from collections import defaultdict
from dataclasses import dataclass

from .config import settings
from .driver import DockerDriver
from .security import agent_api_key, container_name, dispatch_token

logger = logging.getLogger("hermes-dispatch.manager")

driver = DockerDriver()

# 每用户一把锁：同一用户的冷启动/重启串行，避免并发重建
locks: dict[str, asyncio.Lock] = defaultdict(asyncio.Lock)

# 活跃 WS 连接计数：desktop 的长连接本身就是活跃证据
active_ws: dict[str, int] = defaultdict(int)

# 内存活跃度（代理请求路径的快速缓存）；清扫循环周期性与注册表对齐
last_active: dict[str, float] = {}


@dataclass
class AgentEndpoint:
    base_url: str          # dashboard 链路（forwarder :9120 → 环回 dashboard）
    api_base_url: str      # API Server 链路（:8642，/v1 直通代理用）
    api_key: str
    dispatch_tok: str
    # 本次 ensure_ready 是否拉起/重启了容器（冷启动）
    started: bool = False


# 落库任务引用集：防止 create_task 结果被 GC（asyncio 已知坑）
_bg_tasks: set[asyncio.Task] = set()

# 后台预热去重
_warming: set[str] = set()


def warm_async(user_id: str) -> None:
    """后台把容器拉到就绪（探活快速失败后触发，让下一次重试直接命中）。"""
    if user_id in _warming:
        return
    _warming.add(user_id)

    async def _run() -> None:
        try:
            await ensure_ready(user_id)
            logger.info("background warm ready: %s", user_id)
        except Exception as e:
            logger.warning("background warm failed for %s: %s", user_id, e)
        finally:
            _warming.discard(user_id)

    task = asyncio.get_running_loop().create_task(_run())
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


def touch(user_id: str) -> None:
    now = time.time()
    last_active[user_id] = now
    # 尽力而为落库；清扫循环也会对齐，这里失败不影响请求
    from .registry import registry

    task = asyncio.get_running_loop().create_task(registry.touch(user_id, now))
    _bg_tasks.add(task)
    task.add_done_callback(_bg_tasks.discard)


async def flush_active() -> None:
    """把内存活跃度刷入注册表（清扫循环调用；调度服务重启不丢空闲计时）。"""
    from .registry import registry

    for uid, ts in list(last_active.items()):
        db_ts = await registry.get_last_active(uid)
        if db_ts is None or db_ts < ts - 1:
            await registry.touch(uid, ts)


def ws_opened(user_id: str) -> None:
    active_ws[user_id] += 1
    touch(user_id)


def ws_closed(user_id: str) -> None:
    active_ws[user_id] = max(0, active_ws[user_id] - 1)


async def get_last_active(user_id: str) -> float | None:
    if user_id in last_active:
        return last_active[user_id]
    from .registry import registry

    return await registry.get_last_active(user_id)


async def ensure_ready(
    user_id: str,
    restart: bool = False,
    wait: float | None = None,
    token_version: int | None = None,
) -> AgentEndpoint:
    """确保该用户的 agent 容器运行且 dashboard 就绪，返回代理端点。

    restart=True 用于连接失败后的恢复：强制 stop→start→等健康。
    wait：健康等待上限，默认 start_timeout_seconds；代理路径传更短的
    proxy_start_wait_seconds 以免把 desktop 的探针拖死。
    token_version：派生容器内两把 key 的版本；缺省时锁内从注册表读取——
    锁内读取与 rotate（同锁）串行，保证轮换后的下一次 provision 一定用新版本。
    """
    started = False
    timeout = wait if wait is not None else float(settings.start_timeout_seconds)
    async with locks[user_id]:
        if token_version is None:
            from .registry import registry

            user = await registry.get_user(user_id)
            token_version = user.token_version if user else 0
        st = await driver.status(user_id)
        if restart and st.state == "running":
            logger.info("restart requested for %s", container_name(user_id))
            await driver.stop(user_id)
            st = await driver.status(user_id)
        if st.state != "running":
            st = await driver.provision(user_id, token_version=token_version)
            started = True

        ip = await driver.wait_healthy(user_id, timeout)
        if ip is not None:
            # 首启初始化：写入模型配置并重启容器（幂等；完成后需重新等健康）
            if await driver.ensure_agent_config(user_id):
                ip = await driver.wait_healthy(user_id, timeout)
        if ip is None and timeout >= 30:
            # 容器在跑但 dashboard 挂死（或首次启动超时）：重启一轮再试。
            # 短等待（探活快速失败，wait<30）不重试——由后台预热接力
            logger.warning("agent %s not healthy, restarting once", container_name(user_id))
            await driver.stop(user_id)
            await driver.provision(user_id, token_version=token_version)
            started = True
            ip = await driver.wait_healthy(user_id, timeout)
        if ip is None:
            raise RuntimeError(
                f"agent 容器 {container_name(user_id)} 未在 {timeout:.0f}s 内就绪，"
                "请稍后重试或联系管理员"
            )
        return AgentEndpoint(
            base_url=f"http://{ip}:{settings.forwarder_port}",
            api_base_url=f"http://{ip}:{settings.agent_port}",
            api_key=agent_api_key(settings.secret_key, user_id, token_version),
            dispatch_tok=dispatch_token(settings.secret_key, user_id, token_version),
            started=started,
        )


async def sweep_once() -> list[str]:
    """空闲回收一轮；返回被停机的用户列表。"""
    from datetime import datetime, timezone

    from .registry import registry

    now = time.time()
    stopped: list[str] = []
    for item in await driver.list_managed():
        if item["state"] != "running":
            continue
        user_id = item["user_id"]
        if settings.idle_timeout_minutes <= 0:
            # 全局关闭 = 功能停用，per-user 覆盖不重新打开
            continue
        # per-user 策略：None 跟随全局；0 永不回收（messaging 重的用户）；
        # 正数为自定义分钟上限
        user = await registry.get_user(user_id)
        limit = (
            settings.idle_timeout_minutes
            if user is None or user.idle_timeout_minutes is None
            else user.idle_timeout_minutes
        )
        if limit <= 0:
            continue
        if active_ws.get(user_id, 0) > 0:
            continue
        # StartedAt 是 UTC，必须显式解析（naive .timestamp() 会按本地时区
        # 解释、凭空多出 8 小时"空闲"——A 方案踩过的坑）
        started = item.get("started_at") or ""
        started_ts: float | None = None
        try:
            started_ts = (
                datetime.fromisoformat(started.split(".")[0])
                .replace(tzinfo=timezone.utc)
                .timestamp()
            )
        except ValueError:
            pass
        if started_ts is not None and now - started_ts < 600:
            # 刚（重）启动的容器给 10 分钟保活宽限：防止"重启后被旧 last_active
            # 立刻回收"的抖动（探活不计活跃度，冷启动期间没有新的活跃记录）
            continue
        last = last_active.get(user_id)
        if last is None:
            db_ts = await registry.get_last_active(user_id)
            last = db_ts
        if last is None:
            # 无任何活跃记录：回退到容器启动时间
            if started_ts is None:
                continue
            last = started_ts
        if now - last > limit * 60:
            logger.info(
                "idle timeout (limit=%sm, %.0fs): stopping agent %s",
                limit, now - last, item["name"],
            )
            await driver.stop(user_id)
            last_active.pop(user_id, None)
            stopped.append(user_id)
    return stopped
