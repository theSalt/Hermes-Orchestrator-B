"""按用户派生命名与密钥（无状态，确定性 —— 多实例/调度服务重启后结果一致）。"""

import hashlib
import hmac
import re

USER_ID_RE = re.compile(r"^[a-z0-9][a-z0-9._-]{0,63}$")

# desktop URL / 容器名共用的短标识
PUBLIC_PATHS = frozenset({"/api/health", "/api/status"})


def validate_user_id(user_id: str) -> str:
    """管理 API 建用户时强校验：URL 路径段 + 容器名安全字符。"""
    if not USER_ID_RE.fullmatch(user_id or ""):
        raise ValueError(
            "user_id 须匹配 [a-z0-9][a-z0-9._-]{0,63}（小写字母/数字开头）"
        )
    return user_id


def user_slug(user_id: str) -> str:
    """用户 ID → 8 位十六进制短标识（容器名/目录名后缀）。"""
    return hashlib.sha256(user_id.encode("utf-8")).hexdigest()[:8]


def container_name(user_id: str) -> str:
    """确定性容器名：同一用户永远对应同一容器。

    前缀 hb-（hermes-b）：与 A 方案 orchestrator 的 hermes-<slug> 命名空间隔离，
    防止同名 user（如两端各自冒烟的 alice）撞容器名复用对方旧容器。
    """
    return f"hb-{user_slug(user_id)}"


def dispatch_token(secret_key: str, user_id: str) -> str:
    """该用户连接 desktop → dispatch 的 Bearer token（= 其 agent dashboard 的
    HERMES_DASHBOARD_SESSION_TOKEN，两端同源派生，库里不存明文）。"""
    msg = f"dispatch:{user_id}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def agent_api_key(secret_key: str, user_id: str) -> str:
    """该用户 agent 容器 API server（:8642）的 Bearer key（沿用 A 方案）。"""
    msg = f"agent:{user_id}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def session_key(user_id: str) -> str:
    """长期记忆范围标识（X-Hermes-Session-Key），跨会话稳定（/v1 直通路径用）。"""
    return f"desktop:user:{user_slug(user_id)}"


def check_token(presented: str, expected: str) -> bool:
    """常数时间比较，空 token 直接拒绝。"""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))
