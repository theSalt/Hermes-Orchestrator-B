"""API 鉴权网关与管理端点（monkeypatch 掉 docker/上游，不起真容器）。

环境变量与 FakeRegistry 注入见 tests/conftest.py。
"""

import pytest
from fastapi.testclient import TestClient

from app import manager, proxy
from app.config import settings
from app.main import app
from app.security import dispatch_token, user_slug


@pytest.fixture()
def client(monkeypatch, fake_reg):
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
        captured.setdefault("ready_calls", []).append((user_id, restart, wait))
        return manager.AgentEndpoint(
            base_url="http://10.9.9.9:9120",
            api_base_url="http://10.9.9.9:8642",
            api_key="agent-key",
            dispatch_tok=dispatch_token("test-secret", user_id),
        )

    async def fake_noop(user_id):
        return None

    async def fake_remove(user_id):
        captured.setdefault("removed", []).append(user_id)

    async def fake_purge(user_id):
        captured.setdefault("purged", []).append(user_id)

    async def fake_list_managed():
        return []

    async def fake_logs(user_id, tail=200):
        captured["logs_uid"] = user_id
        captured["logs_tail"] = tail
        return f"log line 1 for {user_id}\nlog line 2 (tail={tail})"

    async def fake_sweep():
        return []

    monkeypatch.setattr(proxy, "proxy_http", fake_proxy_http)
    monkeypatch.setattr(manager, "ensure_ready", fake_ensure_ready)
    # driver 的容器操作一律替换（单元测试不碰 docker daemon）
    monkeypatch.setattr(manager.driver, "purge", fake_purge)
    monkeypatch.setattr(manager.driver, "remove", fake_remove)
    monkeypatch.setattr(manager.driver, "stop", fake_noop)
    monkeypatch.setattr(manager.driver, "list_managed", fake_list_managed)
    monkeypatch.setattr(manager.driver, "logs", fake_logs)
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


def test_subpath_public_path_mode(client, monkeypatch):
    """nginx subpath 模式（HERMES_PUBLIC_PATH=/hermes，nginx 已剥前缀转发）：
    路由/鉴权不变；三处对外语义带前缀——gateway_url / cookie Path / X-Forwarded-Prefix。"""
    c, cap = client
    monkeypatch.setattr(settings, "public_path", "/hermes")
    tok = _mkuser(c, "alice")["token"]

    # 管理 API 返回的 gateway_url 可直接粘贴进 desktop（带 subpath）
    r = c.get(
        "/api/users", headers={"Authorization": "Bearer test-admin-key"}
    )
    assert any(
        u["gateway_url"].endswith("/hermes/u/alice") for u in r.json()["users"]
    )

    # ?token= 首开种 cookie：Path 必须含 subpath，否则浏览器在 /hermes/u/... 下不回带
    r = c.get(f"/u/alice/?token={tok}")
    assert r.status_code == 200
    assert "Path=/hermes/u/alice" in r.headers.get("set-cookie", "")

    # 转发上游的 X-Forwarded-Prefix 带 subpath（dashboard 据此重建资源 URL）
    r = c.get("/u/alice/assets/app.js", headers={"X-Hermes-Session-Token": tok})
    assert r.status_code == 200
    assert cap["headers"]["x-forwarded-prefix"] == "/hermes/u/alice"
    # 内部路由不受外部前缀影响（nginx 已剥掉）
    assert cap["upstream_url"] == "http://10.9.9.9:9120/assets/app.js"


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


# ── 管理台：登录会话 / 新端点 ─────────────────────────────────


def _login(c, key="test-admin-key", path="/admin/api/login"):
    return c.post(path, json={"key": key})


def test_admin_login_sets_cookie(client):
    c, _ = client
    r = _login(c)
    assert r.status_code == 200
    cookie = r.headers.get("set-cookie", "")
    assert "hd_admin=" in cookie
    assert "httponly" in cookie.lower()
    assert "samesite=strict" in cookie.lower()
    assert "path=/" in cookie.lower()
    assert "secure" not in cookie.lower()  # http 直连不加 Secure
    assert "max-age=86400" in cookie.lower()


def test_admin_login_wrong_key_401(client):
    c, _ = client
    assert _login(c, key="wrong").status_code == 401
    assert _login(c, key="").status_code == 401


def test_admin_cookie_auth_works(client):
    c, _ = client
    assert c.get("/api/users").status_code == 401  # 未登录
    _login(c)  # TestClient 会话自动保存 cookie
    assert c.get("/api/users").status_code == 200  # cookie 通道
    assert c.get("/admin/api/users").status_code == 200  # 双前缀挂载


def test_admin_bad_cookie_401(client):
    c, _ = client
    c.cookies.set("hd_admin", "123.garbage-sig")
    assert c.get("/api/users").status_code == 401


def test_bad_bearer_good_cookie_passes(client):
    """双通道 OR 语义：坏 Bearer 落到 cookie 校验，好 cookie 放行。"""
    c, _ = client
    _login(c)
    r = c.get("/api/users", headers={"Authorization": "Bearer wrong-key"})
    assert r.status_code == 200


def test_bearer_still_works_after_login_feature(client):
    """既有 Bearer 通道回归（smoke.sh 兼容）。"""
    c, _ = client
    r = c.get("/api/users", headers={"Authorization": "Bearer test-admin-key"})
    assert r.status_code == 200


def test_admin_page_served_without_auth(client):
    c, _ = client
    r = c.get("/admin", follow_redirects=False)
    assert r.status_code == 307
    # 相对 Location：直连与 nginx subpath 两种形态都能正确归一到 admin/
    assert r.headers["location"] == "admin/"
    # 前端构建产物不在测试环境 → 503 占位页；有产物 → 200。均为 HTML
    r = c.get("/admin/")
    assert r.status_code in (200, 503)
    assert "text/html" in r.headers.get("content-type", "")


def test_logout_clears_cookie(client):
    c, _ = client
    r = c.post("/admin/api/logout")
    assert r.status_code == 200
    assert "max-age=0" in r.headers.get("set-cookie", "").lower()


def test_create_user_view_includes_token_version(client):
    c, _ = client
    body = _mkuser(c, "carol")
    assert body["token_version"] == 0


def test_list_view_does_not_echo_token(client):
    """token 仅创建/轮换时一次性返回；清单不回显（admin key 泄露 ≠ token 泄露）。"""
    c, _ = client
    body = _mkuser(c, "alice")
    assert body["token"]  # 创建响应一次性携带
    users = c.get(
        "/api/users", headers={"Authorization": "Bearer test-admin-key"}
    ).json()["users"]
    assert users and all("token" not in u for u in users)


def test_rotate_changes_token_and_recreates_container(client):
    c, cap = client
    old = _mkuser(c, "alice")
    assert old["token_version"] == 0

    r = c.post(
        "/api/users/alice/token/rotate",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "rotated"
    assert body["token_version"] == 1
    assert body["token"] != old["token"]
    assert body["token"] == dispatch_token("test-secret", "alice", 1)
    assert body["gateway_url"].endswith("/u/alice")
    # 先删容器（数据保留）再落版本
    assert cap["removed"] == ["alice"]

    # 列表视图反映新版本，但不回显 token（仅创建/轮换一次性展示）
    users = c.get(
        "/api/users", headers={"Authorization": "Bearer test-admin-key"}
    ).json()["users"]
    alice = next(u for u in users if u["user_id"] == "alice")
    assert alice["token_version"] == 1
    assert "token" not in alice


def test_rotate_unknown_user_404(client):
    c, _ = client
    r = c.post(
        "/api/users/nobody/token/rotate",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 404


def test_old_token_rejected_after_rotate(client):
    """轮换全链路：旧 token 401，新 token 通过 dispatch 层。"""
    c, _ = client
    old = _mkuser(c, "alice")["token"]
    r = c.post(
        "/api/users/alice/token/rotate",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    new = r.json()["token"]

    assert c.get("/u/alice/", headers={"X-Hermes-Session-Token": old}).status_code == 401
    assert c.get("/u/alice/", headers={"X-Hermes-Session-Token": new}).status_code == 200


def test_logs_endpoint(client):
    c, cap = client
    _mkuser(c, "alice")
    r = c.get(
        "/api/users/alice/logs",
        params={"tail": 5},
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 200
    assert cap["logs_uid"] == "alice"
    assert cap["logs_tail"] == 5
    assert "log line 1 for alice" in r.json()["logs"]

    r = c.get(
        "/api/users/nobody/logs",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 404


def test_logs_tail_out_of_range_422(client):
    c, _ = client
    _mkuser(c, "alice")
    r = c.get(
        "/api/users/alice/logs",
        params={"tail": 5000},
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 422


def test_start_endpoint(client):
    c, cap = client
    _mkuser(c, "alice")
    r = c.post(
        "/api/users/alice/start",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 200
    assert cap["ready_calls"][-1][0] == "alice"  # ensure_ready 被调
    assert cap["ready_calls"][-1][2] == float(settings.proxy_start_wait_seconds)

    r = c.post(
        "/api/users/nobody/start",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 404


def test_start_timeout_returns_503(client, monkeypatch):
    c, _ = client

    async def fail_ready(user_id, restart=False, wait=None):
        raise RuntimeError("agent 容器未就绪")

    monkeypatch.setattr(manager, "ensure_ready", fail_ready)
    _mkuser(c, "alice")
    r = c.post(
        "/api/users/alice/start",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 503
    assert "starting" in r.json()["status"]


def test_restart_unknown_user_404(client):
    c, _ = client
    r = c.post(
        "/api/users/nobody/restart",
        headers={"Authorization": "Bearer test-admin-key"},
    )
    assert r.status_code == 404


def test_overview_counts(client, monkeypatch):
    c, _ = client

    async def fake_list():
        return [
            {"user_id": "alice", "name": "hb-a", "state": "running", "started_at": ""},
            {"user_id": "bob", "name": "hb-b", "state": "exited", "started_at": ""},
        ]

    monkeypatch.setattr(manager.driver, "list_managed", fake_list)
    _mkuser(c, "alice")
    r = c.get("/api/overview", headers={"Authorization": "Bearer test-admin-key"})
    assert r.status_code == 200
    body = r.json()
    assert body["users"] >= 1
    assert body["containers_total"] == 2
    assert body["containers_running"] == 1
    assert body["image"] == settings.image
    assert body["idle_timeout_minutes"] == settings.idle_timeout_minutes


def test_overview_requires_admin(client):
    c, _ = client
    assert c.get("/api/overview").status_code == 401
