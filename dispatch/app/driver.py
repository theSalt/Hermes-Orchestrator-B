"""Docker 后端驱动：每用户一个 hermes-agent 容器（overlay 镜像，含 dashboard-forwarder）。

职责：确定性命名、种子目录、创建/启动/停止/删除、健康等待。
不做路由与鉴权（见 manager.py / proxy.py）。
改造自 A 方案 hermes-orchestrator 的 docker_driver.py；差异：
  - 镜像为 overlay（s6 增加 dashboard-forwarder 服务）
  - 注入 dashboard 三件套 env（HERMES_DASHBOARD / 环回绑定 / per-user session token）
  - 就绪探针改为 dashboard 链路（forwarder :9120 → dashboard /api/health）
"""

import asyncio
import logging
import os
import shutil
import time
from dataclasses import asdict, dataclass
from pathlib import Path

import docker
import httpx
from docker.errors import NotFound

from .config import settings
from .security import agent_api_key, container_name, dispatch_token

logger = logging.getLogger("hermes-dispatch.driver")

LABEL_MANAGED = "hermes.dispatch"
LABEL_USER = "hermes.dispatch.user-id"
INIT_MARKER = ".hermes-dispatch-init"
INIT_VERSION = "v1"  # 每次扩展初始化内容时 +1，已有用户会自动重放
HERMES_BIN = "/opt/hermes/.venv/bin/hermes"


@dataclass
class AgentStatus:
    user_id: str
    state: str  # absent | created | running | exited | dead | ...
    container_id: str | None = None
    ip: str | None = None

    def dict(self) -> dict:
        return asdict(self)


class DockerDriver:
    """docker-py 的薄封装；阻塞调用经 asyncio.to_thread 暴露为 async。"""

    def __init__(self) -> None:
        self._client: docker.DockerClient | None = None

    @property
    def client(self) -> docker.DockerClient:
        if self._client is None:
            self._client = docker.from_env()
        return self._client

    # ── 同步实现 ─────────────────────────────────────────────

    def _ensure_network(self) -> None:
        name = settings.network
        found = self.client.networks.list(names=[name])
        if not found:
            logger.info("creating docker network %s", name)
            self.client.networks.create(name, driver="bridge")

    def _status(self, user_id: str) -> AgentStatus:
        try:
            c = self.client.containers.get(container_name(user_id))
        except NotFound:
            return AgentStatus(user_id=user_id, state="absent")
        nets = (c.attrs.get("NetworkSettings") or {}).get("Networks") or {}
        net = nets.get(settings.network) or next(iter(nets.values()), {})
        return AgentStatus(
            user_id=user_id,
            state=(c.attrs.get("State") or {}).get("Status", "unknown"),
            container_id=c.id,
            ip=net.get("IPAddress") or None,
        )

    def _seed_dir(self, user_id: str) -> str:
        """确保该用户的数据目录存在并写入首启种子；返回目录绝对路径。"""
        agent_dir = Path(settings.data_dir) / container_name(user_id)
        agent_dir.mkdir(parents=True, exist_ok=True)
        (agent_dir / "workspace").mkdir(parents=True, exist_ok=True)
        try:
            os.chmod(agent_dir, settings.data_dir_mode)
        except OSError:
            logger.warning("chmod %s failed", agent_dir)
        env_file = agent_dir / ".env"
        if not env_file.exists():
            lines = ["# seeded by hermes-dispatch"]
            lines += [f"{k}={v}" for k, v in sorted(settings.extra_agent_env.items())]
            env_file.write_text("\n".join(lines) + "\n", encoding="utf-8")
        return str(agent_dir)

    def _create_or_start(self, user_id: str, token_version: int = 0) -> AgentStatus:
        name = container_name(user_id)
        self._seed_dir(user_id)
        self._ensure_network()

        env = {
            # API Server（脚本/程序化访问面，沿用 A 方案）
            "API_SERVER_ENABLED": "true",
            "API_SERVER_HOST": "0.0.0.0",
            "API_SERVER_PORT": str(settings.agent_port),
            "API_SERVER_KEY": agent_api_key(settings.secret_key, user_id, token_version),
            # Dashboard（desktop 的协议面）：必须绑环回——非环回绑定会强制启用
            # auth gate 且 WS 拒绝 ?token=（token 模式失效）。经 overlay 的
            # dashboard-forwarder(:9120) 在 docker 内网暴露，dispatch 反代。
            "HERMES_DASHBOARD": "1",
            "HERMES_DASHBOARD_HOST": "127.0.0.1",
            "HERMES_DASHBOARD_PORT": str(settings.dashboard_port),
            "HERMES_DASHBOARD_SESSION_TOKEN": dispatch_token(
                settings.secret_key, user_id, token_version
            ),
            "HERMES_DASH_FWD_PORT": str(settings.forwarder_port),
        }
        if settings.pypi_mirror:
            # hermes 首启 lazy-install 走 uv/pip；默认源在国内网络会拖垮冷启动
            env.update(
                {
                    "PIP_INDEX_URL": settings.pypi_mirror,
                    "UV_DEFAULT_INDEX": settings.pypi_mirror,
                    "UV_INDEX_URL": settings.pypi_mirror,
                    "UV_HTTP_TIMEOUT": "60",
                }
            )
        # 物理机/无 IMDS 环境：botocore(bedrock 插件)探测 169.254.169.254 会长时间挂起
        # gateway 启动。默认禁用；需要 IMDS 的环境用 EXTRA_ENV 覆盖。
        env.setdefault("AWS_EC2_METADATA_DISABLED", "true")
        # 显式注入的变量最后覆盖以上默认值
        env.update(settings.extra_agent_env)

        kwargs: dict = {
            "name": name,
            "detach": True,
            "command": "gateway run",
            "network": settings.network,
            "environment": env,
            "volumes": {
                str(Path(settings.data_host_dir) / name): {
                    "bind": "/opt/data",
                    "mode": "rw",
                }
            },
            "labels": {LABEL_MANAGED: "1", LABEL_USER: user_id},
            "restart_policy": {"Name": "unless-stopped"},
            "log_config": docker.types.LogConfig(
                type=docker.types.LogConfig.types.JSON, config={"max-size": "20m", "max-file": "3"}
            ),
        }
        try:
            cpu = float(settings.cpu_limit)
        except ValueError:
            cpu = 0.0
        if cpu > 0:
            kwargs["nano_cpus"] = int(cpu * 1e9)
        if settings.mem_limit:
            kwargs["mem_limit"] = settings.mem_limit

        try:
            c = self.client.containers.get(name)
            labels = (c.attrs.get("Config") or {}).get("Labels") or {}
            if labels.get(LABEL_MANAGED) != "1":
                # 非本服务创建的同名容器（如 A 方案旧容器/残留）：删掉重建。
                # 绝不能复用——镜像/env/卷/网络都与我们约定不符。
                logger.warning(
                    "container %s exists but is not dispatch-managed (labels=%s); recreating",
                    name,
                    labels,
                )
                c.remove(force=True)
                raise NotFound("foreign container removed")
            if (c.attrs.get("State") or {}).get("Status") != "running":
                logger.info("starting existing container %s", name)
                c.start()
        except NotFound:
            logger.info("creating container %s (image=%s)", name, settings.image)
            c = self.client.containers.run(settings.image, **kwargs)
        return self._status(user_id)

    def _stop(self, user_id: str) -> None:
        try:
            c = self.client.containers.get(container_name(user_id))
        except NotFound:
            return
        if (c.attrs.get("State") or {}).get("Status") == "running":
            logger.info("stopping container %s", c.name)
            c.stop(timeout=20)

    def _remove(self, user_id: str) -> None:
        try:
            c = self.client.containers.get(container_name(user_id))
        except NotFound:
            return
        logger.info("removing container %s (data kept)", c.name)
        c.remove(force=True)

    def _purge(self, user_id: str) -> None:
        self._remove(user_id)
        name = container_name(user_id)
        root = Path(settings.data_dir).resolve()
        target = (root / name).resolve()
        # 防御：只允许删除 data_dir 下一级的本服务容器目录（hb-*）
        if target.is_dir() and target.parent == root and target.name.startswith("hb-"):
            logger.info("purging data dir %s", target)
            shutil.rmtree(target)

    def _logs(self, user_id: str, tail: int = 200) -> str:
        try:
            c = self.client.containers.get(container_name(user_id))
        except NotFound:
            return ""
        try:
            out = c.logs(tail=tail)
        except docker.errors.APIError as e:
            logger.warning("logs failed for %s: %s", container_name(user_id), e)
            return ""
        return out.decode("utf-8", errors="replace") if isinstance(out, bytes) else str(out)

    def _list_managed(self) -> list[dict]:
        out = []
        for c in self.client.containers.list(
            all=True, filters={"label": f"{LABEL_MANAGED}=1"}
        ):
            labels = (c.attrs.get("Config") or {}).get("Labels") or {}
            out.append(
                {
                    "user_id": labels.get(LABEL_USER, ""),
                    "name": c.name,
                    "state": (c.attrs.get("State") or {}).get("Status", "unknown"),
                    "started_at": (c.attrs.get("State") or {}).get("StartedAt", ""),
                }
            )
        return out

    # ── async 对外接口 ───────────────────────────────────────

    async def status(self, user_id: str) -> AgentStatus:
        return await asyncio.to_thread(self._status, user_id)

    async def provision(self, user_id: str, token_version: int = 0) -> AgentStatus:
        return await asyncio.to_thread(self._create_or_start, user_id, token_version)

    async def stop(self, user_id: str) -> None:
        await asyncio.to_thread(self._stop, user_id)

    async def remove(self, user_id: str) -> None:
        """删容器，保留数据目录（下次访问冷启动恢复）。"""
        await asyncio.to_thread(self._remove, user_id)

    async def purge(self, user_id: str) -> None:
        """删容器并清空数据目录（记忆/会话一并清除）。"""
        await asyncio.to_thread(self._purge, user_id)

    async def list_managed(self) -> list[dict]:
        return await asyncio.to_thread(self._list_managed)

    async def logs(self, user_id: str, tail: int = 200) -> str:
        """最近 tail 行容器日志（管理台查看）；容器不存在返回空串。"""
        return await asyncio.to_thread(self._logs, user_id, tail)

    async def wait_healthy(self, user_id: str, timeout: float) -> str | None:
        """轮询直至 dashboard（forwarder → 环回 dashboard）的 /api/health 返回 200。

        这是 desktop 的协议面就绪信号；API Server(:8642) 随 gateway 启动，
        不作为就绪依据。返回可用 ip 或 None。
        """
        deadline = time.monotonic() + timeout
        # dashboard 有 DNS-rebinding 防护：Host 头必须是环回名，否则 400。
        # forwarder 只做 TCP 转发，不改写请求，因此 Host 要在这里重写。
        host_header = {"Host": f"127.0.0.1:{settings.dashboard_port}"}
        async with httpx.AsyncClient() as client:
            while time.monotonic() < deadline:
                st = await self.status(user_id)
                if st.state == "running" and st.ip:
                    try:
                        r = await client.get(
                            f"http://{st.ip}:{settings.forwarder_port}/api/health",
                            headers=host_header,
                            timeout=3,
                        )
                        if r.status_code == 200:
                            return st.ip
                    except httpx.HTTPError:
                        pass
                await asyncio.sleep(2)
        return None

    # ── 首启模型配置 ─────────────────────────────────────────
    # hermes 首启会用自带模板无条件覆盖 config.yaml，文件种子行不通；
    # 因此在首次健康检查通过后，用官方 CLI `hermes config set` 写模型配置，
    # 再重启容器使其生效（配置在 gateway 启动时读取）。同 A 方案。

    def _ensure_agent_config(self, user_id: str) -> bool:
        """返回 True 表示执行了配置并重启了容器（调用方需重新等健康）。"""
        name = container_name(user_id)
        marker = Path(settings.data_dir) / name / INIT_MARKER
        if marker.exists() and marker.read_text(encoding="utf-8").strip() == INIT_VERSION:
            return False
        c = self.client.containers.get(name)
        sets: list[tuple[str, str]] = [
            ("terminal.cwd", "/opt/data/workspace"),
        ]
        if settings.model_name:
            sets += [
                ("model.provider", settings.model_provider),
                ("model.model", settings.model_name),
                ("model.default", settings.model_name),
            ]
            if settings.model_base_url:
                sets.append(("model.base_url", settings.model_base_url))
            if settings.model_api_mode:
                sets.append(("model.api_mode", settings.model_api_mode))
        for key, val in sets:
            r = c.exec_run([HERMES_BIN, "config", "set", key, val], user="hermes")
            if r.exit_code != 0:
                logger.warning("config set %s failed on %s: %s", key, name, r.output)
        logger.info("agent init config applied for %s; restarting container", name)
        c.restart(timeout=20)
        marker.write_text(INIT_VERSION + "\n", encoding="utf-8")
        return True

    async def ensure_agent_config(self, user_id: str) -> bool:
        return await asyncio.to_thread(self._ensure_agent_config, user_id)
