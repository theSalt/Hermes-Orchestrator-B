"""security.py：派生函数与 token 校验。"""

import pytest

from app.security import (
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
