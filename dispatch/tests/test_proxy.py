"""proxy.py：头过滤 / URL 构造 / WS URL 拼接（纯函数部分）。"""

from app.proxy import (
    HOP_BY_HOP,
    build_upstream_ws_url,
    filter_request_headers,
    forwarded_headers,
    _filter_response_headers,
)
from app.config import settings


def test_filter_request_headers_strips_hop_by_hop_and_host():
    headers = [
        ("Host", "192.168.0.117:8644"),
        ("X-Hermes-Session-Token", "tok"),
        ("Connection", "keep-alive"),
        ("Transfer-Encoding", "chunked"),
        ("Content-Type", "application/json"),
    ]
    extra = {"Host": "127.0.0.1:9119", "X-Forwarded-Prefix": "/u/alice"}
    out = filter_request_headers(headers, extra)
    assert out["x-hermes-session-token"] == "tok"
    assert out["content-type"] == "application/json"
    assert "host" not in out or out["host"] == "127.0.0.1:9119"
    for h in HOP_BY_HOP:
        assert h not in out or h == "host"
    assert out["x-forwarded-prefix"] == "/u/alice"


def test_forwarded_headers():
    out = forwarded_headers("alice", "http", "10.0.0.1")
    assert out["Host"] == f"127.0.0.1:{settings.dashboard_port}"
    assert out["X-Forwarded-Prefix"] == "/u/alice"
    assert out["X-Forwarded-Proto"] == "http"
    assert out["X-Forwarded-For"] == "10.0.0.1"


def test_forwarded_headers_with_public_path(monkeypatch):
    """nginx subpath 模式：X-Forwarded-Prefix 必须带外部前缀（上游据此重建
    SPA 资源 URL，浏览器资源请求经 nginx 时要带上 /hermes 才能路由回来）。"""
    monkeypatch.setattr(settings, "public_path", "/hermes")
    out = forwarded_headers("alice", "http", "10.0.0.1")
    assert out["X-Forwarded-Prefix"] == "/hermes/u/alice"


def test_normalize_path_prefix():
    from app.config import _normalize_path_prefix

    assert _normalize_path_prefix("/hermes") == "/hermes"
    assert _normalize_path_prefix("hermes/") == "/hermes"
    assert _normalize_path_prefix("  /hermes/ ") == "/hermes"
    assert _normalize_path_prefix("") == ""
    assert _normalize_path_prefix("/") == ""


def test_filter_response_headers_keeps_multiple_cookies():
    headers = [
        ("Content-Type", "text/html"),
        ("Set-Cookie", "a=1"),
        ("Set-Cookie", "b=2"),
        ("Transfer-Encoding", "chunked"),
        ("Connection", "close"),
    ]
    out = _filter_response_headers(headers)
    assert out["content-type"] == "text/html"
    assert out["set-cookie"] == ["a=1", "b=2"]
    assert "transfer-encoding" not in out
    assert "connection" not in out


def test_build_upstream_ws_url():
    assert (
        build_upstream_ws_url("http://10.0.0.2:9120", "api/ws", "token=abc")
        == "ws://10.0.0.2:9120/api/ws?token=abc"
    )
    assert build_upstream_ws_url("http://10.0.0.2:9120/", "", "") == "ws://10.0.0.2:9120/"
