#!/usr/bin/env python3
"""dashboard 环回转发器：0.0.0.0:FWD_PORT → 127.0.0.1:DASHBOARD_PORT（含 Host 头改写）。

hermes dashboard 的 token 鉴权模式要求绑定环回（非环回绑定强制启用 auth gate 且
WS 拒绝 ?token=）。但 dashboard 同时有 DNS-rebinding 防护：HTTP 与 WS 握手的
Host 头必须是环回名、WS 对端必须是环回 IP。本转发器在 docker 网内暴露纯 TCP
等价面，并把每个连接首个请求的 Host 头改写为环回（对端天然是环回——上游连接
由本进程发起）。用户级鉴权由 hermes-dispatch 在代理前完成，dashboard 自身的
token 校验兜底。
"""

import asyncio
import os
import re

LISTEN_PORT = int(os.environ.get("HERMES_DASH_FWD_PORT", "9120"))
UPSTREAM_HOST = "127.0.0.1"
UPSTREAM_PORT = int(os.environ.get("HERMES_DASHBOARD_PORT", "9119"))
MAX_CONNS = int(os.environ.get("HERMES_DASH_FWD_MAX_CONNS", "64"))

# Host 头改写上限：请求头总长超过此值视为异常（正常握手远小于此）
_HEADER_CAP = 16384
# 连行尾一起匹配替换（$ 会吞掉 \r 导致裸 LF，h11 会拒）
_HOST_RE = re.compile(rb"(?im)^Host:[^\r\n]*\r?\n")
_HOST_REWRITE = f"Host: {UPSTREAM_HOST}:{UPSTREAM_PORT}\r\n".encode()

active = 0


async def pump(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.IncompleteReadError, TimeoutError):
        pass
    finally:
        try:
            writer.close()
            await writer.wait_closed()
        except (ConnectionError, OSError):
            pass


async def handle(client_reader: asyncio.StreamReader, client_writer: asyncio.StreamWriter) -> None:
    global active
    if active >= MAX_CONNS:
        client_writer.close()
        return
    active += 1
    try:
        try:
            up_reader, up_writer = await asyncio.wait_for(
                asyncio.open_connection(UPSTREAM_HOST, UPSTREAM_PORT), timeout=5
            )
        except (OSError, asyncio.TimeoutError):
            client_writer.close()
            return

        # 缓冲首个请求头，改写 Host 后连同已有负载一起发给上游。
        # HTTP keep-alive 复用同一连接的第二跳请求由调度服务侧保证不会出现
        # （dispatch 每请求新建上游连接；WS 每连接只有一个握手）。
        buf = b""
        while b"\r\n\r\n" not in buf and len(buf) < _HEADER_CAP:
            chunk = await client_reader.read(4096)
            if not chunk:
                break
            buf += chunk
        if buf:
            head, sep, rest = buf.partition(b"\r\n\r\n")
            head = _HOST_RE.sub(_HOST_REWRITE, head)
            up_writer.write(head + sep + rest)
            await up_writer.drain()

        await asyncio.gather(
            pump(client_reader, up_writer),
            pump(up_reader, client_writer),
            return_exceptions=True,
        )
    finally:
        active -= 1


async def main() -> None:
    server = await asyncio.start_server(handle, "0.0.0.0", LISTEN_PORT)
    print(
        f"[dashboard-forwarder] listening :{LISTEN_PORT} -> {UPSTREAM_HOST}:{UPSTREAM_PORT}"
        f" (Host rewritten to {_HOST_REWRITE.decode()})",
        flush=True,
    )
    async with server:
        await server.serve_forever()


if __name__ == "__main__":
    asyncio.run(main())
