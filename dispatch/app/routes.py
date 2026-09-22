"""HTTP/WS 路由：鉴权网关、desktop 反向代理、管理端点、/v1 直通代理。

注册顺序敏感：/v1 直通与 /api 管理路由必须先于 /u/{uid}/{rest:path} 兜底代理。
"""

import logging
import re
import time
from pathlib import Path
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Query, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import (
    FileResponse,
    HTMLResponse,
    JSONResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)

from . import manager, proxy
from .config import settings
from .registry import User, registry
from .security import (
    PUBLIC_PATHS,
    admin_session_issue,
    admin_session_verify,
    check_token,
    container_name,
    dispatch_token,
    session_key,
    user_slug,
    validate_user_id,
)

logger = logging.getLogger("hermes-dispatch.api")
router = APIRouter()

HTTP_METHODS = ["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"]
PROXY_TIMEOUT = httpx.Timeout(float(settings.proxy_timeout_seconds), connect=10.0)


# ── 鉴权 ─────────────────────────────────────────────────────


def _presented_token(request: Request) -> str:
    """desktop 的三种携带形态：专用头 / Bearer / ?token=（下载链接与 WS 用）。"""
    tok = request.headers.get("x-hermes-session-token", "").strip()
    if tok:
        return tok
    auth = request.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    return request.query_params.get("token", "").strip()


def cookie_name(user_id: str) -> str:
    """浏览器会话 cookie 名（按用户隔离，Path 限定 /u/<user_id>）。"""
    return f"hd_{user_slug(user_id)}"


def session_cookie(user_id: str, token: str) -> str:
    """浏览器访问 Web 控制台用的会话 cookie。

    首次用 ?token= 打开 /u/<uid>/ 后种下；后续 <script>/fetch/WS 等浏览器
    原生请求不带自定义头，凭此 cookie 放行。HttpOnly + SameSite=Lax。
    Path 含外部 subpath 前缀（nginx /hermes/ 场景），否则浏览器不会回带。
    """
    return (
        f"{cookie_name(user_id)}={token}; Path={settings.public_path}/u/{user_id}; "
        "HttpOnly; SameSite=Lax; Max-Age=2592000"
    )


def _split_user_path(path: str) -> tuple[str, str]:
    """/u/<user_id>[/<rest>] → (user_id, rest)；不合法时 user_id 为空串。"""
    parts = path.lstrip("/").split("/", 2)
    if len(parts) < 2 or parts[0] != "u":
        return "", ""
    user_id = parts[1]
    rest = parts[2] if len(parts) > 2 else ""
    try:
        validate_user_id(user_id)
    except ValueError:
        return "", ""
    return user_id, rest


async def _authorize(request: Request) -> tuple[str, str, User | None]:
    """提取 user_id 并校验 dispatch token；公开探活路径放行。

    返回 (user_id, stripped_path, user)，失败抛 HTTPException。
    token 按该用户当前的 token_version 派生——轮换后旧 token 立即失效。
    """
    user_id, stripped = _split_user_path(request.scope["path"])
    if not user_id:
        raise HTTPException(status_code=404, detail="invalid user id")
    # PUBLIC_PATHS 带前导斜杠；stripped 无斜杠，归一后比较
    if f"/{stripped}" in PUBLIC_PATHS:
        # 上游本就公开的探活路径（无敏感信息）；desktop 启动握手需要匿名可达
        return user_id, stripped, None
    user = await registry.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    expected = dispatch_token(settings.secret_key, user_id, user.token_version)
    presented = _presented_token(request) or request.cookies.get(cookie_name(user_id), "")
    if not check_token(presented, expected):
        raise HTTPException(status_code=401, detail="invalid or missing session token")
    return user_id, stripped, user


async def require_admin(request: Request) -> None:
    """管理鉴权双通道：Bearer admin key（curl/脚本）或登录会话 cookie（管理台）。"""
    auth = request.headers.get("authorization") or ""
    key = auth.removeprefix("Bearer ").strip()
    if key and check_token(key, settings.admin_key):
        return
    if admin_session_verify(
        settings.secret_key,
        settings.admin_key,
        request.cookies.get(ADMIN_COOKIE, ""),
        ttl_seconds=settings.admin_session_ttl_seconds,
    ):
        return
    raise HTTPException(status_code=401, detail="invalid or missing admin credentials")


# ── 基础 ─────────────────────────────────────────────────────


@router.get("/health")
async def health():
    return {"status": "ok", "service": "hermes-dispatch"}


# ── /v1 直通代理（程序化调用，沿用 A 方案协议；先于 /u 兜底注册）──


async def _chat_proxy(user_id: str, body: dict, chat_id: Optional[str]):
    manager.touch(user_id)
    stream = bool(body.get("stream"))

    def _agent_headers(ep: manager.AgentEndpoint) -> dict:
        return {
            "Authorization": f"Bearer {ep.api_key}",
            "X-Hermes-Session-Key": session_key(user_id),
            **({"X-Hermes-Session-Id": chat_id[:128]} if chat_id else {}),
        }

    async def send(ep: manager.AgentEndpoint):
        client = httpx.AsyncClient(timeout=PROXY_TIMEOUT)
        try:
            req = client.build_request(
                "POST",
                f"{ep.api_base_url}/v1/chat/completions",
                json=body,
                headers=_agent_headers(ep),
            )
            resp = await client.send(req, stream=True)
            return client, resp
        except BaseException:
            await client.aclose()
            raise

    try:
        ep = await manager.ensure_ready(user_id)
        client, resp = await send(ep)
    except httpx.ConnectError:
        logger.warning("connect failed for %s, restarting container", container_name(user_id))
        try:
            ep = await manager.ensure_ready(user_id, restart=True)
            client, resp = await send(ep)
        except (httpx.ConnectError, RuntimeError) as e:
            return JSONResponse(
                {"error": {"message": str(e), "type": "agent_unavailable"}}, status_code=503
            )
    except RuntimeError as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": "agent_unavailable"}}, status_code=503
        )

    if resp.status_code != 200:
        data = await resp.aread()
        media = resp.headers.get("content-type", "application/json")
        await resp.aclose()
        await client.aclose()
        return Response(content=data, status_code=resp.status_code, media_type=media)

    if not stream:
        data = await resp.aread()
        await resp.aclose()
        await client.aclose()
        return Response(content=data, media_type="application/json")

    async def sse_gen():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        sse_gen(),
        media_type=resp.headers.get("content-type", "text/event-stream"),
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


@router.post("/v1/chat/completions")
async def chat_completions(request: Request):
    """无用户路由：用户取 X-User-Id 头，缺省 default（仅冒烟/管理员用）。"""
    body = await request.json()
    user_id = request.headers.get("x-user-id") or settings.default_user
    chat_id = request.headers.get("x-session-id")
    return await _chat_proxy(user_id, body, chat_id)


@router.post("/u/{user_id}/v1/chat/completions")
async def chat_completions_user(user_id: str, request: Request):
    user = await registry.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    expected = dispatch_token(settings.secret_key, user_id, user.token_version)
    if not check_token(_presented_token(request), expected):
        raise HTTPException(status_code=401, detail="invalid or missing session token")
    body = await request.json()
    chat_id = request.headers.get("x-session-id")
    return await _chat_proxy(user_id, body, chat_id)


# ── desktop 反向代理（HTTP）───────────────────────────────────


def _wait_seconds(stripped: str = "") -> float:
    """冷启动等待上限。

    探活路径（desktop 的鉴权探测只等 8s）用短等待快速返回 503，由其重试；
    其余路径用较长等待（用户实际操作，值得等冷启动）。
    """
    if f"/{stripped}" in PUBLIC_PATHS:
        # desktop 探测超时 8s：等待上限压到 5s，留足网络/处理余量
        return min(5.0, float(settings.proxy_start_wait_seconds))
    return float(settings.proxy_start_wait_seconds)


# Desktop 渲染 agent 消息里的下载链接时会把 markdown 尾渣带进 path 参数
# （上游按字面 404）。已知两类实际案例：
#   ① `...` 代码格式路径 → 结尾反引号：/path/report.pptx`
#   ② agent 把 MEDIA: 标签写成加粗并追加中文标注：
#      **MEDIA:/path/Hermes-Agent-介绍.pptx**（源文件）
#      → path 收到 /path/Hermes-Agent-介绍.pptx**（源文件）
# 正常文件名不会以反引号/星号结尾（* 在 Windows/macOS 为非法文件名字符）；
# 结尾括号标注只在「括号前的主干以扩展名收尾」时才剥——report（终稿）.docx
# 这类括号后还有扩展名的真实文件名不受影响。
_TRAILING_LABEL_RE = re.compile(r"[（(][^()（）/\\]{0,32}[）)]$")
_EXT_SUFFIX_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_PATH_QUERY_TRIGGER_CHARS = ("`", "*", "(", ")", "（", "）",
                             "%60", "%2a", "%28", "%29", "%ef%bc%88", "%ef%bc%89")


def _clean_media_path(value: str) -> str:
    """剥掉 path 值结尾的 markdown 尾渣（反引号/星号/括号标注），循环至稳定。"""
    prev = None
    while prev != value:
        prev = value
        value = value.rstrip("`*")
        m = _TRAILING_LABEL_RE.search(value)
        if m and _EXT_SUFFIX_RE.search(value[: m.start()].rstrip("`*")):
            value = value[: m.start()]
    return value


def _sanitize_path_query(query: str) -> str:
    lowered = query.lower()
    if not any(ch in lowered for ch in _PATH_QUERY_TRIGGER_CHARS):
        return query
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(query, keep_blank_values=True)
    changed = False
    fixed = []
    for k, v in pairs:
        if k == "path":
            cleaned = _clean_media_path(v)
            if cleaned != v:
                v = cleaned
                changed = True
        fixed.append((k, v))
    if changed:
        logger.info("stripped trailing markdown residue from path param (desktop rendering quirk)")
        return urlencode(fixed)
    return query


async def _proxy_http_entry(request: Request):
    method = request.method
    # CORS 预检放行（无用户数据，上游 CORSMiddleware 应答）
    preflight = method == "OPTIONS"
    if preflight:
        user_id, stripped = _split_user_path(request.scope["path"])
        if not user_id:
            raise HTTPException(status_code=404, detail="invalid user id")
        user = None
    else:
        user_id, stripped, user = await _authorize(request)
    # 任何鉴权请求都算活跃（匿名探活与 CORS 预检不算——登出的 desktop 轮询不能钉住容器）
    if method not in ("OPTIONS", "HEAD") and f"/{stripped}" not in PUBLIC_PATHS:
        manager.touch(user_id)

    # 用 ?token=/头鉴权（而非 cookie）时，顺手种浏览器会话 cookie：
    # 用户首次打开 /u/<uid>/?token=... 后，SPA 的静态资源/接口/WS 即可凭 cookie 访问
    wants_cookie = not preflight and f"/{stripped}" not in PUBLIC_PATHS and bool(
        _presented_token(request)
    )

    extra = proxy.forwarded_headers(
        user_id, request.url.scheme, request.client.host if request.client else ""
    )
    headers = proxy.filter_request_headers(request.headers, extra)
    body_stream = request.stream()

    async def attempt(ep: manager.AgentEndpoint):
        upstream_url = f"{ep.base_url}/{stripped}"
        if request.url.query:
            upstream_url += f"?{_sanitize_path_query(request.url.query)}"
        return await proxy.proxy_http(method, upstream_url, headers=headers, request_stream=body_stream)

    try:
        ep = await manager.ensure_ready(user_id, wait=_wait_seconds(stripped))
        resp = await attempt(ep)
    except httpx.ConnectError:
        # 容器在跑但连接不上（IP 变化/gateway 挂死）→ 重启一轮后重试一次
        logger.warning("connect failed for %s, restarting container", container_name(user_id))
        try:
            ep = await manager.ensure_ready(user_id, restart=True, wait=_wait_seconds(stripped))
            resp = await attempt(ep)
        except (httpx.ConnectError, RuntimeError) as e:
            if f"/{stripped}" in PUBLIC_PATHS:
                manager.warm_async(user_id)  # 后台接力预热，重试即可命中
            return JSONResponse(
                {"error": {"message": str(e), "type": "agent_unavailable"}}, status_code=503
            )
    except RuntimeError as e:
        if f"/{stripped}" in PUBLIC_PATHS:
            # 探活路径快速失败后，后台继续把容器拉到就绪——desktop 的下一次
            # 重试（或 waitForHermesReady 轮询）即可命中，不用干等长超时
            manager.warm_async(user_id)
            return JSONResponse({"status": "starting", "detail": str(e)}, status_code=503)
        return JSONResponse(
            {"error": {"message": str(e), "type": "agent_unavailable"}}, status_code=503
        )
    except httpx.HTTPError as e:
        return JSONResponse(
            {"error": {"message": str(e), "type": "agent_unavailable"}}, status_code=502
        )
    if wants_cookie and user is not None:
        resp.headers.append(
            "set-cookie",
            session_cookie(
                user_id, dispatch_token(settings.secret_key, user_id, user.token_version)
            ),
        )
    return resp


@router.api_route("/u/{user_id}", methods=HTTP_METHODS)
@router.api_route("/u/{user_id}/", methods=HTTP_METHODS)
@router.api_route("/u/{user_id}/{rest:path}", methods=HTTP_METHODS)
async def proxy_http_route(request: Request, user_id: str, rest: str = ""):
    return await _proxy_http_entry(request)


# ── desktop 反向代理（WebSocket）──────────────────────────────


def _ws_presented_token(ws: WebSocket, user_id: str) -> str:
    tok = ws.headers.get("x-hermes-session-token", "").strip()
    if tok:
        return tok
    auth = ws.headers.get("authorization", "")
    if auth.lower().startswith("bearer "):
        return auth[7:].strip()
    tok = ws.query_params.get("token", "").strip()
    if tok:
        return tok
    # 浏览器同源 WS 握手自动携带 cookie
    return ws.cookies.get(cookie_name(user_id), "").strip()


@router.websocket("/u/{user_id}/{rest:path}")
@router.websocket("/u/{user_id}")
async def proxy_ws_route(ws: WebSocket, user_id: str, rest: str = ""):
    """desktop 的主传输通道（JSON-RPC over /api/ws 等）。

    握手阶段完成鉴权（未通过则拒绝升级，不发 accept）。
    """
    user = await registry.get_user(user_id)
    if user is None:
        await ws.close(code=1008)
        return
    expected = dispatch_token(settings.secret_key, user_id, user.token_version)
    if not check_token(_ws_presented_token(ws, user_id), expected):
        # 握手 403：desktop 按连接失败重试，与上游 gate 拒绝行为一致
        await ws.close(code=1008)
        return

    await ws.accept()
    manager.ws_opened(user_id)
    try:
        try:
            # WS 是 desktop 启动序列的最后一步（探活通过后才拨），可以等久一点
            ep = await manager.ensure_ready(user_id, wait=_wait_seconds(rest))
        except RuntimeError as e:
            logger.warning("ws ensure_ready failed for %s: %s", user_id, e)
            await ws.close(code=1011, reason="agent not ready")
            return
        manager.touch(user_id)
        try:
            await proxy.proxy_ws(
                ws,
                ep,
                rest,
                ws.url.query,
                subprotocols=list(ws.scope.get("subprotocols", [])),
            )
        except (WebSocketDisconnect, Exception) as e:  # noqa: BLE001 - 泵内异常统一收尾
            logger.debug("ws proxy ended for %s: %r", user_id, e)
    finally:
        manager.ws_closed(user_id)


# ── 管理后台 ─────────────────────────────────────────────────
#
# 管理 API 定义在 admin_api 上，双前缀挂载（见文件末尾 include_router）：
#   /api/...       —— 内网直连/脚本（smoke.sh 等既有调用方，保持兼容）
#   /admin/api/... —— 管理台 UI 专用：与页面同前缀，UI 用相对路径请求，
#                     直连与 nginx subpath 两种部署形态都自动正确。
# 公网 nginx 对 /hermes/api/ 维持 403，仅放行 /hermes/admin/*（见 deploy/）。

# 管理台构建产物（dispatch/web 经 vite 构建后 COPY 进镜像）
WEB_DIST = Path(__file__).parent / "static" / "admin"
ADMIN_COOKIE = "hd_admin"


def _gateway_url_for(request: Request, user_id: str) -> str:
    if settings.public_url:
        return f"{settings.public_url}{settings.public_path}/u/{user_id}"
    host = request.headers.get("host") or f"localhost:{settings.port}"
    scheme = request.headers.get("x-forwarded-proto") or "http"
    return f"{scheme}://{host}{settings.public_path}/u/{user_id}"


def _user_view(request: Request, user: User, agent: dict | None, include_token: bool = False) -> dict:
    last = agent.get("last_active") if agent else None
    view = {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "slug": container_name(user.user_id),
        "gateway_url": _gateway_url_for(request, user.user_id),
        # token 只在创建/轮换时一次性返回（include_token=True），清单不回显：
        # admin key 泄露不应等于存量 token 泄露；遗忘 token 走轮换
        "token_version": user.token_version,
        # None=跟随全局；0=永不回收；正数=自定义分钟上限
        "idle_timeout_minutes": user.idle_timeout_minutes,
        "container": agent,
        "idle_seconds": int(time.time() - last) if last else None,
    }
    if include_token:
        view["token"] = dispatch_token(settings.secret_key, user.user_id, user.token_version)
    return view


def _proto(request: Request) -> str:
    """真实外部协议：优先信任反代头（nginx 已设 X-Forwarded-Proto）。"""
    return (request.headers.get("x-forwarded-proto") or request.url.scheme).lower()


def _admin_cookie(request: Request) -> str:
    issued = int(time.time())
    val = admin_session_issue(settings.secret_key, settings.admin_key, issued)
    parts = [
        f"{ADMIN_COOKIE}={val}",
        "Path=/",
        "HttpOnly",
        # Strict：跨站请求不带 cookie（CSRF 防线）；外部链接跳入需重登一次
        "SameSite=Strict",
        f"Max-Age={settings.admin_session_ttl_seconds}",
    ]
    if _proto(request) == "https":
        parts.append("Secure")
    return "; ".join(parts)


admin_api = APIRouter()


@admin_api.post("/login", include_in_schema=False)
async def admin_login(request: Request):
    """管理台登录：校验 admin key，种 HttpOnly 会话 cookie。"""
    body = await request.json() if request.headers.get("content-length") else {}
    if not check_token(str(body.get("key") or ""), settings.admin_key):
        raise HTTPException(status_code=401, detail="invalid admin key")
    return JSONResponse({"status": "ok"}, headers={"set-cookie": _admin_cookie(request)})


@admin_api.post("/logout", include_in_schema=False)
async def admin_logout(request: Request):
    cookie = f"{ADMIN_COOKIE}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"
    if _proto(request) == "https":
        cookie += "; Secure"
    return JSONResponse({"status": "ok"}, headers={"set-cookie": cookie})


@admin_api.get("/overview", dependencies=[Depends(require_admin)])
async def overview():
    """服务概览：用户/容器计数与关键配置（管理台顶栏卡片）。"""
    users = await registry.list_users()
    agents = await manager.driver.list_managed()
    return {
        "users": len(users),
        "containers_total": len(agents),
        "containers_running": sum(1 for a in agents if a["state"] == "running"),
        "active_ws": sum(manager.active_ws.values()),
        "image": settings.image,
        "network": settings.network,
        "idle_timeout_minutes": settings.idle_timeout_minutes,
        "public_url": settings.public_url,
        "public_path": settings.public_path,
        "default_user": settings.default_user,
        "session_ttl_seconds": settings.admin_session_ttl_seconds,
    }


@admin_api.get("/users", dependencies=[Depends(require_admin)])
async def list_users(request: Request):
    users = await registry.list_users()
    agents = {a["user_id"]: a for a in await manager.driver.list_managed()}
    out = []
    for u in users:
        agent = agents.get(u.user_id)
        if agent:
            agent = dict(agent)
            agent["last_active"] = await manager.get_last_active(u.user_id)
            agent["active_ws"] = manager.active_ws.get(u.user_id, 0)
        out.append(_user_view(request, u, agent))
    return {"users": out}


@admin_api.post("/users", dependencies=[Depends(require_admin)])
async def create_user(request: Request):
    body = await request.json() if request.headers.get("content-length") else {}
    user_id = str(body.get("user_id") or "").strip()
    display_name = str(body.get("display_name") or "").strip()
    try:
        validate_user_id(user_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    try:
        user = await registry.create_user(user_id, display_name)
    except KeyError as e:
        raise HTTPException(status_code=409, detail=str(e).strip("'"))
    # 唯一一次回显 token 的入口（另一个是 rotate）
    return _user_view(request, user, None, include_token=True)


@admin_api.delete("/users/{user_id}", dependencies=[Depends(require_admin)])
async def delete_user(user_id: str, request: Request, purge: bool = False):
    user = await registry.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    if purge:
        await manager.driver.purge(user_id)
    else:
        await manager.driver.remove(user_id)
    await registry.delete_user(user_id)
    manager.last_active.pop(user_id, None)
    return {"status": "deleted", "data": "deleted" if purge else "kept"}


@admin_api.post("/users/{user_id}/start", dependencies=[Depends(require_admin)])
async def start_agent(user_id: str):
    """把容器拉到就绪（等 proxy_start_wait_seconds，超时 503 可重试）。"""
    if await registry.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    try:
        await manager.ensure_ready(user_id, wait=float(settings.proxy_start_wait_seconds))
    except RuntimeError as e:
        return JSONResponse({"status": "starting", "detail": str(e)}, status_code=503)
    return {"status": "ok"}


@admin_api.post("/users/{user_id}/restart", dependencies=[Depends(require_admin)])
async def restart_agent(user_id: str):
    if await registry.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    await manager.ensure_ready(user_id, restart=True)
    return {"status": "ok"}


@admin_api.post("/users/{user_id}/stop", dependencies=[Depends(require_admin)])
async def stop_agent(user_id: str):
    await manager.driver.stop(user_id)
    return {"status": "stopped"}


@admin_api.get("/users/{user_id}/logs", dependencies=[Depends(require_admin)])
async def user_logs(user_id: str, tail: int = Query(200, ge=1, le=1000)):
    if await registry.get_user(user_id) is None:
        raise HTTPException(status_code=404, detail="unknown user")
    return {
        "user_id": user_id,
        "tail": tail,
        "logs": await manager.driver.logs(user_id, tail=tail),
    }


@admin_api.post("/users/{user_id}/token/rotate", dependencies=[Depends(require_admin)])
async def rotate_user_token(user_id: str, request: Request):
    """单用户 token 轮换：token_version+1 并重派生。

    dispatch token 同时是容器 env 里的 dashboard 会话 token（创建时写入，
    不可原地变更），因此必须先删容器（数据保留）再落版本；顺序不能反——
    反序时若删容器失败，会出现"新版本已生效、旧 token 容器仍在跑"的卡死态。
    全程持该用户的锁，与 ensure_ready 的 provision 串行。
    """
    user = await registry.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    async with manager.locks[user_id]:
        await manager.driver.remove(user_id)  # absent 时 no-op；失败则中止不落版本
        version = await registry.rotate_token_version(user_id)
    return {
        "status": "rotated",
        "token_version": version,
        "token": dispatch_token(settings.secret_key, user_id, version),
        "gateway_url": _gateway_url_for(request, user_id),
        "note": "容器已删除（数据保留）；用户需在 desktop 更新 token 后重连",
    }


@admin_api.put("/users/{user_id}/idle-timeout", dependencies=[Depends(require_admin)])
async def set_idle_timeout(user_id: str, request: Request):
    """空闲回收策略：null=跟随全局；0=永不回收；正数=自定义分钟上限。"""
    body = await request.json() if request.headers.get("content-length") else {}
    raw = body.get("minutes", None)
    if raw is not None:
        if not isinstance(raw, int) or isinstance(raw, bool) or raw < 0:
            raise HTTPException(
                status_code=400, detail="minutes 须为 null（跟随全局）、0（永不回收）或正整数分钟"
            )
    user = await registry.set_idle_timeout(user_id, raw)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    return {
        "status": "ok",
        "idle_timeout_minutes": user.idle_timeout_minutes,
        "effective_minutes": (
            settings.idle_timeout_minutes
            if user.idle_timeout_minutes is None
            else user.idle_timeout_minutes
        ),
        "note": "0=永不回收；null=跟随全局 HERMES_IDLE_TIMEOUT_MINUTES",
    }


@admin_api.post("/agents/refresh", dependencies=[Depends(require_admin)])
async def refresh_agents(request: Request):
    """镜像/配置变更后生效：删容器（数据保留），下次访问按新配置重建。"""
    body = await request.json() if request.headers.get("content-length") else {}
    only_idle = bool(body.get("only_idle", True))
    idle_minutes = int(body.get("idle_minutes") or 5)
    now = time.time()
    removed = []
    for it in await manager.driver.list_managed():
        uid = it["user_id"]
        if only_idle and it["state"] == "running":
            last = await manager.get_last_active(uid)
            if last and now - last < idle_minutes * 60:
                continue
            if manager.active_ws.get(uid, 0) > 0:
                continue
        await manager.driver.remove(uid)
        removed.append(uid)
    return {"status": "ok", "removed": removed}


# ── 管理台页面（构建产物静态服务）──────────────────────────────


@router.get("/admin", include_in_schema=False)
async def admin_page():
    """无尾斜杠归一：/admin → admin/（相对 Location——直连与 nginx subpath
    两种形态下浏览器都能解析到正确的 /[/hermes]/admin/，且不受
    HERMES_PUBLIC_PATH 影响：该前缀经 nginx 时已被剥掉）。"""
    return RedirectResponse("admin/", status_code=307)


@router.get("/admin/", include_in_schema=False)
async def admin_page_index():
    index = WEB_DIST / "index.html"
    if index.is_file():
        return FileResponse(index, media_type="text/html; charset=utf-8")
    return HTMLResponse(
        "<h1>hermes-dispatch 管理台</h1>"
        "<p>前端构建产物缺失：在 dispatch/web 下执行 <code>npm install &amp;&amp; npm run build</code> "
        "后重新构建镜像（Dockerfile 多阶段构建会自动完成）。</p>",
        status_code=503,
    )


@router.get("/admin/assets/{rest:path}", include_in_schema=False)
async def admin_assets(rest: str):
    assets_root = (WEB_DIST / "assets").resolve()
    target = (assets_root / rest).resolve()
    # 防路径穿越：只允许 assets 目录内的真实文件
    if assets_root not in target.parents or not target.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return FileResponse(target)


# 管理路由挂载：/api/* 兼容既有脚本；/admin/api/* 供管理台相对路径使用
router.include_router(admin_api, prefix="/api")
router.include_router(admin_api, prefix="/admin/api")

