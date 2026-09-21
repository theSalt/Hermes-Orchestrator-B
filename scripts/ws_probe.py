#!/usr/bin/env python3
"""WS 探针：验证 desktop 的主传输通道（JSON-RPC over /api/ws）经 dispatch 可用。

用法：python3 scripts/ws_probe.py <gateway_url> <token>
例：  python3 scripts/ws_probe.py http://192.168.0.117:8644/u/alice <token>

验证点：
  1. ?token= 握手通过 dispatch → forwarder → dashboard 全链路
  2. 连接保持打开（gateway 的 /api/ws 不会立刻拒绝）
  3. 能收到服务端初始化帧（若有）
"""

import asyncio
import sys
import urllib.parse
import urllib.request

import websockets


async def main() -> int:
    if len(sys.argv) < 3:
        print(__doc__)
        return 2
    base_url, token = sys.argv[1], sys.argv[2]

    # 1) 先走一遍匿名探活（desktop 启动握手同款）
    status_url = base_url.rstrip("/") + "/api/status"
    with urllib.request.urlopen(status_url, timeout=15) as resp:
        print(f"[probe] GET /api/status -> {resp.status}")
        body = resp.read().decode("utf-8", "replace")
        print(f"[probe] {body[:200]}")

    parsed = urllib.parse.urlparse(base_url)
    ws_scheme = "wss" if parsed.scheme == "https" else "ws"
    ws_url = (
        f"{ws_scheme}://{parsed.netloc}{parsed.path.rstrip('/')}"
        f"/api/ws?token={urllib.parse.quote(token)}"
    )
    print(f"[ws] connecting {ws_url}")

    async with websockets.connect(ws_url, max_size=None, open_timeout=30) as ws:
        print("[ws] connected")

        async def recv_some():
            for _ in range(3):
                try:
                    msg = await asyncio.wait_for(ws.recv(), timeout=5)
                    print(f"[ws] <- {str(msg)[:160]}")
                except asyncio.TimeoutError:
                    print("[ws] (5s 无下行帧，保持连接)")
                    return

        await recv_some()
        # 发一个无害的 ping 文本帧（协议层 JSON-RPC 解析错误也会回错误帧——
        # 只要不是立刻断连，就证明链路与鉴权 OK）
        await ws.send("not-json")
        print('[ws] -> "not-json" (预期服务端回协议错误帧而非断连)')
        try:
            msg = await asyncio.wait_for(ws.recv(), timeout=10)
            print(f"[ws] <- {str(msg)[:160]}")
        except asyncio.TimeoutError:
            print("[ws] (无响应，但连接未断)")
        print("[ws] OK: 握手/传输链路验证通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
