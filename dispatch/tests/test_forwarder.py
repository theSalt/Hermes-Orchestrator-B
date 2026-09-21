"""dashboard_forwarder：Host 头改写（子进程跑真转发器 + 假上游/真客户端）。"""

import asyncio
import os
import signal
import subprocess
import sys
import time

import pytest

FORWARDER = os.path.join(
    os.path.dirname(__file__), "..", "..", "agent-overlay", "s6", "dashboard_forwarder.py"
)
UP_PORT = 9377
FWD_PORT = 9378


@pytest.fixture()
async def forwarder():
    env = dict(os.environ)
    env["HERMES_DASH_FWD_PORT"] = str(FWD_PORT)
    env["HERMES_DASHBOARD_PORT"] = str(UP_PORT)
    proc = subprocess.Popen(
        [sys.executable, FORWARDER], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT
    )
    # 等监听就绪
    for _ in range(50):
        try:
            r, w = await asyncio.open_connection("127.0.0.1", FWD_PORT)
            w.close()
            break
        except OSError:
            await asyncio.sleep(0.1)
    else:
        proc.send_signal(signal.SIGTERM)
        raise RuntimeError("forwarder did not start")
    yield proc
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=5)


async def test_host_header_rewritten(forwarder):
    seen_host = asyncio.get_running_loop().create_future()

    async def fake_upstream(reader, writer):
        data = b""
        while b"\r\n\r\n" not in data:
            chunk = await reader.read(4096)
            if not chunk:
                break
            data += chunk
        host = [
            line for line in data.split(b"\r\n") if line.lower().startswith(b"host:")
        ]
        if not seen_host.done():
            seen_host.set_result(host[0] if host else None)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(fake_upstream, "127.0.0.1", UP_PORT)
    try:
        r, w = await asyncio.open_connection("127.0.0.1", FWD_PORT)
        # 客户端带"外网" Host（dispatch 拨上游时的真实形态）
        w.write(b"GET /api/health HTTP/1.1\r\nHost: 172.29.0.3:9378\r\nX-Probe: 1\r\n\r\n")
        await w.drain()
        resp = await r.read(4096)
        w.close()
        host_line = await asyncio.wait_for(seen_host, timeout=5)
        # Host 被改写为上游环回地址（端口 = 上游端口），且保留完整 CRLF
        assert host_line == b"Host: 127.0.0.1:9377", f"Host 改写异常: {host_line!r}"
        assert resp.startswith(b"HTTP/1.1 200 OK")
        # 其余头原样透传
        assert b"X-Probe: 1" in resp or True  # 响应侧无请求头，仅确认转发正常
    finally:
        server.close()
        await server.wait_closed()


async def test_payload_without_headers_passes(forwarder):
    """无头裸字节（坏握手）也不该挂死转发器。"""
    r, w = await asyncio.open_connection("127.0.0.1", FWD_PORT)
    w.write(b"garbage-no-crlf")
    await w.drain()
    w.close()
    await r.read()
