"""按用户派生命名与密钥（无状态，确定性 —— 多实例/调度服务重启后结果一致）。"""

import hashlib
import hmac
import re
import time

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


def dispatch_token(secret_key: str, user_id: str, version: int = 0) -> str:
    """该用户连接 desktop → dispatch 的 Bearer token（= 其 agent dashboard 的
    HERMES_DASHBOARD_SESSION_TOKEN，两端同源派生，库里不存明文）。

    version=0 保持初代消息（存量 token 不变）；管理台轮换 token 即
    token_version+1，消息变为 dispatch:<uid>#v<n>。
    """
    msg = f"dispatch:{user_id}" if version <= 0 else f"dispatch:{user_id}#v{version}"
    return hmac.new(secret_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def agent_api_key(secret_key: str, user_id: str, version: int = 0) -> str:
    """该用户 agent 容器 API server（:8642）的 Bearer key（沿用 A 方案）。

    与 dispatch_token 共用 version，保持"容器 env 两把 key 同源同版"的不变量。
    """
    msg = f"agent:{user_id}" if version <= 0 else f"agent:{user_id}#v{version}"
    return hmac.new(secret_key.encode("utf-8"), msg.encode("utf-8"), hashlib.sha256).hexdigest()


def session_key(user_id: str) -> str:
    """长期记忆范围标识（X-Hermes-Session-Key），跨会话稳定（/v1 直通路径用）。"""
    return f"desktop:user:{user_slug(user_id)}"


def check_token(presented: str, expected: str) -> bool:
    """常数时间比较，空 token 直接拒绝。"""
    if not presented or not expected:
        return False
    return hmac.compare_digest(presented.encode("utf-8"), expected.encode("utf-8"))


# ── 管理后台登录会话（HMAC 签名，无服务端状态）──────────────────


def _admin_session_sig(secret_key: str, admin_key: str, issued_at: int) -> str:
    # 签名同时绑定 secret_key 与 admin_key（哈希后入消息）：任一 key 轮换，
    # 全体已签发会话立即失效
    ak = hashlib.sha256(admin_key.encode("utf-8")).hexdigest()
    msg = f"admin-session:{ak}:{issued_at}".encode("utf-8")
    return hmac.new(secret_key.encode("utf-8"), msg, hashlib.sha256).hexdigest()


def admin_session_issue(secret_key: str, admin_key: str, issued_at: int) -> str:
    """签发会话值 "<issued_at>.<sig>"；存于浏览器 cookie，服务端无状态。"""
    return f"{issued_at}.{_admin_session_sig(secret_key, admin_key, issued_at)}"


def admin_session_verify(
    secret_key: str,
    admin_key: str,
    value: str,
    ttl_seconds: int,
    now: float | None = None,
) -> bool:
    """校验会话值；过期/篡改/格式非法/未来时间戳一律拒绝。"""
    try:
        issued_s, sig = value.split(".", 1)
        issued = int(issued_s)
    except (ValueError, AttributeError):
        return False
    current = time.time() if now is None else now
    if issued > current + 60:  # 未来时间戳拒绝（容忍 60s 时钟偏差）
        return False
    if current - issued > ttl_seconds:
        return False
    return hmac.compare_digest(
        sig.encode("utf-8"),
        _admin_session_sig(secret_key, admin_key, issued).encode("utf-8"),
    )
