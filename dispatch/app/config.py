"""Environment-driven settings for hermes-dispatch.

所有配置项通过 HERMES_ 前缀环境变量注入，见仓库根目录 .env.example。
"""

import json
import logging
import os

logger = logging.getLogger("hermes-dispatch.config")


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, "") or default)
    except ValueError:
        return default


def _env_json(name: str, default: dict | None = None) -> dict:
    raw = os.environ.get(name)
    if not raw:
        return dict(default or {})
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else dict(default or {})
    except json.JSONDecodeError:
        logger.warning("%s is not valid JSON, using default", name)
        return dict(default or {})


def _env_octal(name: str, default: str) -> int:
    try:
        return int(os.environ.get(name, "") or default, 8)
    except ValueError:
        return 0o777


def _normalize_path_prefix(raw: str) -> str:
    """/hermes、hermes/、//hermes/ → /hermes；空串/根 → 空串（无前缀）。"""
    p = "/" + raw.strip().strip("/")
    return "" if p == "/" else p


class Settings:
    """运行配置；在进程启动时读取一次。"""

    def __init__(self) -> None:
        # ── 认证 ──────────────────────────────────────────────
        # 管理端点（用户管理/运维）的 Bearer Key
        self.admin_key: str = os.environ.get("HERMES_ADMIN_KEY", "dev-admin-key")
        # 派生每用户 dispatch token / agent API key 的 HMAC 密钥（生产必须改掉默认值）
        self.secret_key: str = os.environ.get("HERMES_SECRET_KEY", "dev-secret-change-me")
        # 管理后台登录会话的有效期（秒）；换 admin_key 即全体管理会话失效
        self.admin_session_ttl_seconds: int = _env_int(
            "HERMES_ADMIN_SESSION_TTL_SECONDS", 86400
        )

        # ── 服务 ──────────────────────────────────────────────
        self.port: int = _env_int("HERMES_PORT", 8644)
        # 对外公布的 gateway URL 前缀（管理 API 返回给用户粘贴进 desktop）。
        # 置空则按请求的 Host 反推。
        self.public_url: str = os.environ.get("HERMES_PUBLIC_URL", "").rstrip("/")
        # 反代 subpath 前缀（如经 nginx /hermes/ 访问时设 /hermes）。nginx 剥掉该
        # 前缀后转发；dispatch 用它修正 X-Forwarded-Prefix / cookie Path / gateway_url。
        self.public_path: str = _normalize_path_prefix(
            os.environ.get("HERMES_PUBLIC_PATH", "")
        )

        # ── agent 运行时 ─────────────────────────────────────
        # overlay 镜像（基线 + dashboard-forwarder），见 agent-overlay/
        self.image: str = os.environ.get("HERMES_IMAGE", "hermes-agent:desktop")
        self.agent_port: int = _env_int("HERMES_AGENT_PORT", 8642)
        # dashboard 在容器内绑定环回；forwarder 把它暴露到容器内网接口
        self.dashboard_port: int = _env_int("HERMES_DASHBOARD_PORT", 9119)
        self.forwarder_port: int = _env_int("HERMES_DASH_FWD_PORT", 9120)
        self.network: str = os.environ.get("HERMES_NETWORK", "hermes-b-net")
        self.cpu_limit: str = os.environ.get("HERMES_CPU_LIMIT", "2")
        self.mem_limit: str = os.environ.get("HERMES_MEM_LIMIT", "2g")

        # ── 存储 ─────────────────────────────────────────────
        # users 注册表（PostgreSQL）。compose 侧默认由 POSTGRES_* 组装，
        # 直跑/特殊部署时可整体覆盖
        self.database_url: str = os.environ.get("HERMES_DATABASE_URL", "")
        # dispatch 容器视角的数据根目录（写种子文件等）
        self.data_dir: str = os.environ.get("HERMES_DATA_DIR", "/data/DockerVolume/hermes-b")
        # agent 容器 bind mount 的源路径（= 宿主机视角；单卷双挂时两者相同）
        self.data_host_dir: str = (
            os.environ.get("HERMES_DATA_HOST_DIR") or self.data_dir
        )
        self.data_dir_mode: int = _env_octal("HERMES_DATA_DIR_MODE", "777")

        # ── 生命周期 ─────────────────────────────────────────
        self.idle_timeout_minutes: int = _env_int("HERMES_IDLE_TIMEOUT_MINUTES", 60)
        self.start_timeout_seconds: int = _env_int("HERMES_START_TIMEOUT_SECONDS", 240)
        self.sweep_interval_seconds: int = _env_int("HERMES_SWEEP_INTERVAL_SECONDS", 30)
        # 冷启动等待上限：代理请求最多阻塞这么久等容器就绪，超时 503
        self.proxy_start_wait_seconds: int = _env_int("HERMES_PROXY_START_WAIT_SECONDS", 90)

        # ── 代理 ─────────────────────────────────────────────
        # 代理请求的响应体/请求体透传超时（长聊天/大文件）
        self.proxy_timeout_seconds: int = _env_int("HERMES_PROXY_TIMEOUT_SECONDS", 600)
        # WS 代理单帧上限（MB）；websockets 客户端侧不设限，uvicorn 侧用此值
        self.ws_max_size_mb: int = _env_int("HERMES_WS_MAX_SIZE_MB", 128)

        # ── 直连 API 兜底（/v1/chat/completions 直通代理）──
        self.default_user: str = os.environ.get("HERMES_DEFAULT_USER", "default")

        # ── 额外注入 agent 容器的环境变量（JSON），如模型 Key ──
        self.extra_agent_env: dict = _env_json("HERMES_AGENT_EXTRA_ENV")

        # ── agent 容器内 uv/pip 的 PyPI 镜像（hermes 首启 lazy-install 插件依赖）──
        self.pypi_mirror: str = os.environ.get(
            "HERMES_PYPI_MIRROR", "https://pypi.tuna.tsinghua.edu.cn/simple"
        )

        # ── 模型配置种子（首启时写入该用户的 config.yaml；同 A 方案）──
        self.model_provider: str = os.environ.get("HERMES_MODEL_PROVIDER", "openai-api")
        self.model_name: str = os.environ.get("HERMES_MODEL_NAME", "")
        self.model_base_url: str = os.environ.get("HERMES_MODEL_BASE_URL", "")
        self.model_api_mode: str = os.environ.get("HERMES_MODEL_API_MODE", "")

        # ── 附件上限（/v1 附件直通用；desktop 走 dashboard 自带上传通道）──
        self.max_file_mb: int = _env_int("HERMES_MAX_FILE_MB", 50)

        if not os.path.isabs(self.data_host_dir):
            logger.warning(
                "HERMES_DATA_HOST_DIR=%r 不是绝对路径；容器内运行时 bind mount 会失败",
                self.data_host_dir,
            )


settings = Settings()
