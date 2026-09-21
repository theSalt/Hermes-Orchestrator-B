"""反向代理：desktop ↔ dispatch ↔ per-user agent dashboard。

两条链路：
  - HTTP 流式代理（httpx 双向流）
  - WebSocket 双向泵（websockets 客户端，text/binary/close 全透传）

协议要点（详见 docs/design.md §2）：
  - 上游 dashboard 绑定容器内环回，Host 头必须重写为环回，否则触发其
    DNS-rebinding 防护（host_header_middleware 直接 400）
  - 注入 X-Forwarded-Prefix，dashboard 官方机制（dashboard_auth/prefix.py）
    据此重建 SPA 资源 URL/cookie path
  - hop-by-hop 头两端剥离；鉴权头（X-Hermes-Session-Token / ?token=）原样透传，
    上游自身再做一次 token 校验（纵深防御）
"""

import asyncio
import logging
from typing import Optional

import httpx
import websockets
from starlette.responses import StreamingResponse
from websockets.asyncio.client import ClientConnection

from .config import settings

logger = logging.getLogger("hermes-dispatch.proxy")

# RFC 9110/2616 逐跳头：两端都不透传
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "proxy-connection",
    }
)


def _iter_headers(headers):
    """兼容 starlette/httpx 多值头（.items() 含重复键）与 [(k, v)] 列表。"""
    items = headers.items() if hasattr(headers, "items") else headers
    for k, v in items:
        if isinstance(v, list):
            for one in v:
                yield k, one
        else:
            yield k, v


def filter_request_headers(headers, extra: dict) -> dict:
    out = {}
    for k, v in _iter_headers(headers):
        lk = k.lower()
        if lk in HOP_BY_HOP or lk in ("host", "content-length"):
            continue
        out[lk] = v
    for k, v in extra.items():
        out[k.lower()] = v
    return out


def _filter_response_headers(headers) -> dict:
    out = {}
    for k, v in _iter_headers(headers):
        lk = k.lower()
        if lk in HOP_BY_HOP or lk == "content-length":
            continue
        # 多个 Set-Cookie 必须逐个保留
        if lk == "set-cookie":
            out.setdefault("set-cookie", []).append(v)
            continue
        out[lk] = v
    return out


def forwarded_headers(user_id: str, proto: str, client_host: str) -> dict:
    """每次代理注入的头：Host 重写 + Forwarded 语义。"""
    return {
        "Host": f"127.0.0.1:{settings.dashboard_port}",
        "X-Forwarded-Prefix": f"/u/{user_id}",
        "X-Forwarded-Proto": proto,
        "X-Forwarded-For": client_host,
    }


async def proxy_http(
    method: str,
    upstream_url: str,
    headers: dict,
    request_stream,
    status_code_cb=None,
):
    """把一个已鉴权的 HTTP 请求流式代理到上游，返回 StreamingResponse。"""
    timeout = httpx.Timeout(float(settings.proxy_timeout_seconds), connect=10.0)
    client = httpx.AsyncClient(timeout=timeout)

    async def request_bytes():
        async for chunk in request_stream:
            yield chunk

    try:
        req = client.build_request(
            method,
            upstream_url,
            headers=headers,
            content=request_bytes(),
        )
        resp = await client.send(req, stream=True)
    except BaseException:
        await client.aclose()
        raise
    if status_code_cb:
        status_code_cb(resp.status_code)

    async def body_iter():
        try:
            async for chunk in resp.aiter_bytes():
                yield chunk
        finally:
            await resp.aclose()
            await client.aclose()

    return StreamingResponse(
        body_iter(),
        status_code=resp.status_code,
        media_type=resp.headers.get("content-type"),
        headers=_filter_response_headers(resp.headers),
    )


# ── WebSocket 代理 ───────────────────────────────────────────


def build_upstream_ws_url(base_url: str, path: str, query: str) -> str:
    base = base_url.rstrip("/")
    for scheme in ("https://", "http://"):
        if base.startswith(scheme):
            base = base[len(scheme):]
            break
    p = f"/{path.lstrip('/')}" if path else "/"
    return f"ws://{base}{p}" + (f"?{query}" if query else "")


async def proxy_ws(
    ws,  # starlette WebSocket
    ep,  # manager.AgentEndpoint
    path: str,
    query: str,
    subprotocols: list[str],
) -> None:
    """已鉴权的 WS：拨上游 → 双向泵 → 透传关闭码。

    任意一端先断，用其关闭码/原因关闭另一端；上游连接失败回 1011。
    """
    upstream_url = build_upstream_ws_url(ep.base_url, path, query)
    try:
        # Host 头不在这里改写：websockets 的 Headers 是追加语义，会发出重复
        # Host（dashboard 直接 403）。Host 改写由 forwarder 在 TCP 侧完成。
        upstream: ClientConnection = await websockets.connect(
            upstream_url,
            max_size=None,
            subprotocols=subprotocols or None,
        )
    except (OSError, websockets.InvalidURI, websockets.InvalidHandshake) as e:
        logger.warning("ws upstream dial failed %s: %s", upstream_url, e)
        await ws.close(code=1011, reason="upstream unavailable")
        return

    async def starlette_to_upstream():
        try:
            while True:
                msg = await ws.receive()
                if msg["type"] == "websocket.disconnect":
                    await upstream.close(code=msg.get("code") or 1000)
                    return
                if msg["type"] == "websocket.receive":
                    if msg.get("text") is not None:
                        await upstream.send(msg["text"])
                    elif msg.get("bytes") is not None:
                        await upstream.send(msg["bytes"])
        except (websockets.ConnectionClosed, asyncio.CancelledError):
            raise
        except Exception:
            await upstream.close(code=1011)

    async def upstream_to_starlette():
        try:
            async for message in upstream:
                if isinstance(message, str):
                    await ws.send_text(message)
                else:
                    await ws.send_bytes(message)
            await ws.close(code=1000)
        except Exception:
            try:
                await ws.close(code=1011)
            except Exception:
                pass

    try:
        done, pending = await asyncio.wait(
            [
                asyncio.create_task(starlette_to_upstream()),
                asyncio.create_task(upstream_to_starlette()),
            ],
            return_when=asyncio.FIRST_COMPLETED,
        )
        if pending:
            # 一端先断（通常是上游 EOF）：给另一端短暂冲刷窗口，
            # 避免在途帧（如 client 刚连上就发的首帧）被取消丢弃
            _, pending = await asyncio.wait(pending, timeout=1.0)
        for t in pending:
            t.cancel()
        for t in done:
            exc = t.exception()
            if exc and not isinstance(exc, asyncio.CancelledError):
                logger.debug("ws pump ended with %r", exc)
    finally:
        try:
            await upstream.close()
        except Exception:
            pass
