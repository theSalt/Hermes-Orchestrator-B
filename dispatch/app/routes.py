"""HTTP/WS 路由：鉴权网关、desktop 反向代理、管理端点、/v1 直通代理。

注册顺序敏感：/v1 直通与 /api 管理路由必须先于 /u/{uid}/{rest:path} 兜底代理。
"""

import logging
import time
from typing import Optional

import httpx
from fastapi import APIRouter, Depends, HTTPException, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

from . import manager, proxy
from .config import settings
from .registry import registry
from .security import (
    PUBLIC_PATHS,
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


async def _authorize(request: Request) -> tuple[str, str]:
    """提取 user_id 并校验 dispatch token；公开探活路径放行。

    返回 (user_id, stripped_path)，失败抛 HTTPException。
    """
    user_id, stripped = _split_user_path(request.scope["path"])
    if not user_id:
        raise HTTPException(status_code=404, detail="invalid user id")
    # PUBLIC_PATHS 带前导斜杠；stripped 无斜杠，归一后比较
    if f"/{stripped}" in PUBLIC_PATHS:
        # 上游本就公开的探活路径（无敏感信息）；desktop 启动握手需要匿名可达
        return user_id, stripped
    user = await registry.get_user(user_id)
    if user is None:
        raise HTTPException(status_code=404, detail="unknown user")
    expected = dispatch_token(settings.secret_key, user_id)
    presented = _presented_token(request) or request.cookies.get(cookie_name(user_id), "")
    if not check_token(presented, expected):
        raise HTTPException(status_code=401, detail="invalid or missing session token")
    return user_id, stripped


async def require_admin(request: Request) -> None:
    auth = request.headers.get("authorization") or ""
    key = auth.removeprefix("Bearer ").strip()
    if not key or key != settings.admin_key:
        raise HTTPException(status_code=401, detail="invalid or missing admin key")


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
    if not check_token(_presented_token(request), dispatch_token(settings.secret_key, user_id)):
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


def _strip_backtick_path(query: str) -> str:
    """Desktop 把 agent 消息里 `...` 代码格式的文件路径渲染成下载链接时，
    会把结尾反引号带进 path 参数（上游按字面 404）。正常文件名不会以反引号
    结尾，这里剥掉并留痕。"""
    if "`" not in query and "%60" not in query:
        return query
    from urllib.parse import parse_qsl, urlencode

    pairs = parse_qsl(query, keep_blank_values=True)
    changed = False
    fixed = []
    for k, v in pairs:
        if k == "path" and v.rstrip("`") != v:
            v = v.rstrip("`")
            changed = True
        fixed.append((k, v))
    if changed:
        logger.info("stripped trailing backtick(s) from path param (desktop rendering quirk)")
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
    else:
        user_id, stripped = await _authorize(request)
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
            upstream_url += f"?{_strip_backtick_path(request.url.query)}"
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
    if wants_cookie:
        resp.headers.append(
            "set-cookie", session_cookie(user_id, dispatch_token(settings.secret_key, user_id))
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
    expected = dispatch_token(settings.secret_key, user_id)
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


# ── 管理 API ─────────────────────────────────────────────────


def _gateway_url_for(request: Request, user_id: str) -> str:
    if settings.public_url:
        return f"{settings.public_url}{settings.public_path}/u/{user_id}"
    host = request.headers.get("host") or f"localhost:{settings.port}"
    scheme = request.headers.get("x-forwarded-proto") or "http"
    return f"{scheme}://{host}{settings.public_path}/u/{user_id}"


def _user_view(request: Request, user, agent: dict | None) -> dict:
    last = agent.get("last_active") if agent else None
    view = {
        "user_id": user.user_id,
        "display_name": user.display_name,
        "slug": container_name(user.user_id),
        "gateway_url": _gateway_url_for(request, user.user_id),
        # 管理面直接回显 token，便于管理员交付给用户（仅 admin key 可见）
        "token": dispatch_token(settings.secret_key, user.user_id),
        "container": agent,
        "idle_seconds": int(time.time() - last) if last else None,
    }
    return view


@router.get("/api/users", dependencies=[Depends(require_admin)])
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


@router.post("/api/users", dependencies=[Depends(require_admin)])
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
    return _user_view(request, user, None)


@router.delete("/api/users/{user_id}", dependencies=[Depends(require_admin)])
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


@router.post("/api/users/{user_id}/restart", dependencies=[Depends(require_admin)])
async def restart_agent(user_id: str):
    await manager.ensure_ready(user_id, restart=True)
    return {"status": "ok"}


@router.post("/api/users/{user_id}/stop", dependencies=[Depends(require_admin)])
async def stop_agent(user_id: str):
    await manager.driver.stop(user_id)
    return {"status": "stopped"}


@router.post("/api/agents/refresh", dependencies=[Depends(require_admin)])
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
