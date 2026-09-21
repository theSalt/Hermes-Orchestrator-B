"""security.py：派生函数与 token 校验。"""

import pytest

from app.security import (
    admin_session_issue,
    admin_session_verify,
    agent_api_key,
    check_token,
    container_name,
    dispatch_token,
    user_slug,
    validate_user_id,
)


def test_user_slug_deterministic():
    assert user_slug("alice") == user_slug("alice")
    assert user_slug("alice") != user_slug("bob")
    assert len(user_slug("alice")) == 8


def test_container_name():
    # hb- 前缀与 A 方案 orchestrator 的 hermes-<slug> 命名空间隔离
    assert container_name("alice") == f"hb-{user_slug('alice')}"


def test_dispatch_token_differs_per_user_and_from_agent_key():
    tok_a = dispatch_token("s3cret", "alice")
    tok_b = dispatch_token("s3cret", "bob")
    assert tok_a != tok_b
    assert tok_a != agent_api_key("s3cret", "alice")
    assert dispatch_token("s3cret", "alice") == dispatch_token("s3cret", "alice")
    assert dispatch_token("other", "alice") != tok_a


def test_check_token():
    expected = dispatch_token("s3cret", "alice")
    assert check_token(expected, expected)
    assert not check_token("", expected)
    assert not check_token(expected, "")
    assert not check_token(expected[:-1] + ("0" if expected[-1] != "0" else "1"), expected)


def test_token_version_changes_derivation_compatibly():
    """version=0 与无参调用逐字节一致（存量兼容）；>=1 走新消息。"""
    for fn in (dispatch_token, agent_api_key):
        assert fn("s3cret", "alice", 0) == fn("s3cret", "alice")
        assert fn("s3cret", "alice", 1) != fn("s3cret", "alice")
        assert fn("s3cret", "alice", 2) != fn("s3cret", "alice", 1)
    # dispatch 与 agent 两族消息域互不串
    assert dispatch_token("s3cret", "alice", 1) != agent_api_key("s3cret", "alice", 1)


SECRET = "sess-secret"
KEY = "admin-key"


def test_admin_session_roundtrip():
    val = admin_session_issue(SECRET, KEY, 1000)
    assert admin_session_verify(SECRET, KEY, val, ttl_seconds=3600, now=1000)


def test_admin_session_wrong_key_or_secret():
    val = admin_session_issue(SECRET, KEY, 1000)
    assert not admin_session_verify(SECRET, "other-key", val, ttl_seconds=3600, now=1000)
    assert not admin_session_verify("other-secret", KEY, val, ttl_seconds=3600, now=1000)


def test_admin_session_expired():
    val = admin_session_issue(SECRET, KEY, 1000)
    assert admin_session_verify(SECRET, KEY, val, ttl_seconds=3600, now=4600)
    assert not admin_session_verify(SECRET, KEY, val, ttl_seconds=3600, now=4601)


def test_admin_session_malformed():
    for bad in ("", "novalue", "no-dot-here", "abc.def", "99999.zzz"):
        assert not admin_session_verify(SECRET, KEY, bad, ttl_seconds=3600, now=1000)


def test_admin_session_future_timestamp():
    val = admin_session_issue(SECRET, KEY, 2000)
    # 未来 61s：超时钟容差，拒绝
    assert not admin_session_verify(SECRET, KEY, val, ttl_seconds=3600, now=2000 - 61)
    # 未来 30s：容差内放行
    assert admin_session_verify(SECRET, KEY, val, ttl_seconds=3600, now=2000 - 30)


@pytest.mark.parametrize(
    "uid,valid",
    [
        ("alice", True),
        ("a", True),
        ("zhang.san", True),
        ("user-01", True),
        ("", False),
        ("Alice", False),
        ("0abc", True),
        ("-abc", False),
        ("a b", False),
        ("a/b", False),
        ("a" * 64, True),
        ("a" * 65, False),
        ("../etc", False),
    ],
)
def test_validate_user_id(uid, valid):
    if valid:
        assert validate_user_id(uid) == uid
    else:
        with pytest.raises(ValueError):
            validate_user_id(uid)
