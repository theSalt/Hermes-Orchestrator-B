"""API 鉴权网关与管理端点（monkeypatch 掉 docker/上游，不起真容器）。"""

import os
import tempfile

_tmp = tempfile.mkdtemp(prefix="hermes-dispatch-test-")
os.environ.setdefault("HERMES_DATA_DIR", _tmp)
os.environ.setdefault("HERMES_ADMIN_KEY", "test-admin-key")
os.environ.setdefault("HERMES_SECRET_KEY", "test-secret")

import pytest
from fastapi.testclient import TestClient

from app import manager, proxy
from app.main import app
from app.security import dispatch_token, user_slug
from app.registry import registry


@pytest.fixture()
def client(monkeypatch):
    captured = {}

    def fake_proxy_http(method, upstream_url, headers, request_stream, **kw):
        captured["method"] = method
        captured["upstream_url"] = upstream_url
        captured["headers"] = dict(headers)

        async def run():
            from starlette.responses import JSONResponse

            return JSONResponse({"upstream": upstream_url, "ok": True})

        return run()

    async def fake_ensure_ready(user_id, restart=False, wait=None):
        return manager.AgentEndpoint(
            base_url="http://10.9.9.9:9120",
            api_base_url="http://10.9.9.9:8642",
            api_key="agent-key",
            dispatch_tok=dispatch_token("test-secret", user_id),
        )

    async def fake_noop(user_id):
        return None

    async def fake_list_managed():
        return []

    async def fake_sweep():
        return []

    monkeypatch.setattr(proxy, "proxy_http", fake_proxy_http)
    monkeypatch.setattr(manager, "ensure_ready", fake_ensure_ready)
    # driver 的容器操作一律替换（单元测试不碰 docker daemon）
    monkeypatch.setattr(manager.driver, "purge", fake_noop)
    monkeypatch.setattr(manager.driver, "remove", fake_noop)
    monkeypatch.setattr(manager.driver, "stop", fake_noop)
    monkeypatch.setattr(manager.driver, "list_managed", fake_list_managed)
    monkeypatch.setattr(manager, "sweep_once", fake_sweep)
    with TestClient(app) as c:
        captured["client"] = c
        yield c, captured


def _mkuser(client, uid, **kw):
    # 幂等建用户：已存在则删除重建
    r = client.post(
        "/api/users",
        json={"user_id": uid, **kw},
        headers={"Authorization": "Bearer test-admin-key"},
    )
    if r.status_code == 409:
        client.delete(
            f"/api/users/{uid}?purge=1",
            headers={"Authorization": "Bearer test-admin-key"},
        )
        r = client.post(
            "/api/users",
            json={"user_id": uid, **kw},
            headers={"Authorization": "Bearer test-admin-key"},
        )
    assert r.status_code == 200, r.text
    return r.json()


def test_health(client):
    c, _ = client
    r = c.get("/health")
    assert r.status_code == 200
    assert r.json()["status"] == "ok"


def test_admin_requires_key(client):
    c, _ = client
    assert c.get("/api/users").status_code == 401
    assert (
        c.post("/api/users", json={"user_id": "x"}).status_code == 401
    )
    r = c.get("/api/users", headers={"Authorization": "Bearer wrong"})
    assert r.status_code == 401


def test_create_user_returns_gateway_url_and_token(client):
    c, _ = client
    body = _mkuser(c, "alice", display_name="Alice")
    assert body["user_id"] == "alice"
    assert body["gateway_url"].endswith("/u/alice")
    assert body["token"] == dispatch_token("test-secret", "alice")
    assert body["slug"].startswith("hb-")


def test_duplicate_user_conflict(client):
    c, _ = client
    _mkuser(c, "dup")
    r = c.post(
        "/api/users",
        json={"user_id": "dup"},
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 409


def test_invalid_user_id_rejected(client):
    c, _ = client
    r = c.post(
        "/api/users",
        json={"user_id": "Bad/Id"},
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 400


def test_public_probe_paths_anonymous(client):
    c, _ = client
    _mkuser(c, "alice")
    assert c.get("/u/alice/api/health").status_code == 200
    assert c.get("/u/alice/api/status").status_code == 200


def test_auth_gate_and_token_forms(client):
    c, cap = client
    _mkuser(c, "alice")
    _mkuser(c, "bob")
    tok = dispatch_token("test-secret", "alice")
    base = "/u/alice/"

    # 无 token / 错 token / 未知用户
    assert c.get(base).status_code == 401
    assert (
        c.get(base, headers={"X-Hermes-Session-Token": "nope"}).status_code == 401
    )
    assert c.get("/u/nobody/").status_code == 404

    # 三种携带形态
    assert (
        c.get(base, headers={"X-Hermes-Session-Token": tok}).status_code == 200
    )
    assert c.get(base, headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.get(base + "?token=" + tok).status_code == 200

    # 用户隔离：alice 的 token 打 bob → 401
    assert (
        c.get("/u/bob/", headers={"X-Hermes-Session-Token": tok}).status_code == 401
    )


def test_proxy_forwards_with_prefix_and_host_rewrite(client):
    c, cap = client
    tok = _mkuser(c, "alice")["token"]
    r = c.get(
        "/u/alice/api/files?path=/workspace",
        headers={"X-Hermes-Session-Token": tok},
    )
    assert r.status_code == 200
    # 前缀剥离 + query 透传
    assert cap["upstream_url"] == "http://10.9.9.9:9120/api/files?path=/workspace"
    assert cap["headers"]["host"] == "127.0.0.1:9119"
    assert cap["headers"]["x-forwarded-prefix"] == "/u/alice"
    # 客户端鉴权头原样透传（上游二道校验）
    assert cap["headers"]["x-hermes-session-token"] == tok


def test_ws_rejects_without_token(client):
    c, _ = client
    _mkuser(c, "alice")
    with pytest.raises(Exception):
        with c.websocket_connect("/u/alice/api/ws"):
            pass


def test_ws_accepts_with_token(client, monkeypatch):
    c, _ = client
    _mkuser(c, "alice")
    tok = dispatch_token("test-secret", "alice")

    class FakeUpstream:
        def __init__(self):
            self.sent = []
            self.closed = False
            self._frames = ["welcome"]

        async def send(self, msg):
            self.sent.append(msg)

        async def close(self, code=1000):
            self.closed = True

        def __aiter__(self):
            return self

        async def __anext__(self):
            if self._frames:
                return self._frames.pop(0)
            raise StopAsyncIteration

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    fake = FakeUpstream()

    async def fake_connect(url, **kw):
        fake.url = url
        return fake

    import websockets

    monkeypatch.setattr(websockets, "connect", fake_connect)

    with c.websocket_connect(f"/u/alice/api/ws?token={tok}") as ws:
        ws.send_text("hello")
        # 上游回一帧 "welcome" 后结束迭代（服务端泵主动关连接）
        reply = ws.receive_text()
        # 退出 with 时 close 一个已关会话可能报错，容忍之
        try:
            ws.close()
        except Exception:
            pass
    assert reply == "welcome"
    assert fake.sent == ["hello"]
    assert fake.url.startswith("ws://10.9.9.9:9120/api/ws?token=")


def test_v1_route_not_shadowed_by_catchall(client):
    c, _ = client
    tok = _mkuser(c, "alice")["token"]

    async def fake_chat(user_id, body, chat_id):
        from starlette.responses import JSONResponse

        return JSONResponse({"routed_user": user_id})

    import app.routes as routes

    original = routes._chat_proxy
    routes._chat_proxy = fake_chat
    try:
        r = c.post(
            "/u/alice/v1/chat/completions",
            json={"messages": []},
            headers={"X-Hermes-Session-Token": tok},
        )
        assert r.status_code == 200
        assert r.json()["routed_user"] == "alice"
    finally:
        routes._chat_proxy = original


def test_browser_cookie_session(client):
    """?token= 打开首页 → 种 cookie → 后续无头请求（浏览器资源形态）放行。"""
    c, _ = client
    tok = _mkuser(c, "alice")["token"]

    # 1) ?token= 打开根路径 → 200 且 Set-Cookie（HttpOnly + 路径限定）
    r = c.get(f"/u/alice/?token={tok}")
    assert r.status_code == 200
    set_cookie = r.headers.get("set-cookie", "")
    name = f"hd_{user_slug('alice')}"
    assert name in set_cookie and "httponly" in set_cookie.lower()
    assert f"Path=/u/alice" in set_cookie

    # 2) 无 cookie 且无 token → 401；仅凭 cookie（浏览器 <script>/fetch 形态）→ 放行
    c.cookies.clear()  # TestClient 会自动存上一步的 cookie，这里清掉模拟全新浏览器
    r = c.get("/u/alice/assets/index.js")
    assert r.status_code == 401
    c.cookies.set(name, tok)
    r = c.get("/u/alice/assets/index.js")
    assert r.status_code == 200
    r = c.get("/u/alice/api/status")
    assert r.status_code == 200

    # 3) cookie 打到别人路径不行（cookie 按 Path 隔离，dispatch 侧双保险）
    _mkuser(c, "bob")
    r = c.get("/u/bob/", headers={"Cookie": f"{name}={tok}"})
    assert r.status_code == 401


def test_options_preflight_passes_without_auth(client):
    c, _ = client
    _mkuser(c, "alice")
    r = c.options("/u/alice/api/status", headers={"Origin": "http://localhost:3000"})
    assert r.status_code == 200


def test_trailing_backtick_in_path_param_stripped(client):
    """Desktop 渲染下载链接带入结尾反引号 → dispatch 剥掉再转发。"""
    c, cap = client
    tok = _mkuser(c, "alice")["token"]
    r = c.get(
        "/u/alice/api/fs/download",
        params={"path": "/opt/data/workspace/report.pptx`", "profile": "default"},
        headers={"X-Hermes-Session-Token": tok},
    )
    assert r.status_code == 200
    # 反引号被剥掉（urlencode 后为编码形态），其余参数原样
    assert "path=%2Fopt%2Fdata%2Fworkspace%2Freport.pptx" in cap["upstream_url"]
    assert "%60" not in cap["upstream_url"] and "`" not in cap["upstream_url"]
    assert "profile=default" in cap["upstream_url"]


def test_ws_accepts_cookie_auth(client, monkeypatch):
    c, _ = client
    _mkuser(c, "alice")
    tok = _mkuser(c, "alice")["token"]

    class FakeUpstream:
        async def send(self, msg):
            pass

        async def close(self, code=1000):
            pass

        def __aiter__(self):
            return self

        async def __anext__(self):
            raise StopAsyncIteration

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

    async def fake_connect(url, **kw):
        return FakeUpstream()

    import websockets

    monkeypatch.setattr(websockets, "connect", fake_connect)
    c.cookies.set(f"hd_{user_slug('alice')}", tok)
    with c.websocket_connect("/u/alice/api/ws"):
        pass
