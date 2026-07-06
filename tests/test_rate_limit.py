"""Tests for the per-tool MCP invocation rate limiter (MCP spec 2025-11-25's
Tools security considerations: "Servers MUST: ... rate limit tool
invocations")."""

import time

from app.mcp_server.rate_limit import (
    RateLimitExceededError,
    TokenBucket,
    check_rate_limit,
    get_bucket,
    reset_all_buckets,
)


def test_bucket_starts_full():
    bucket = TokenBucket(capacity=5, refill_per_second=1.0)
    for _ in range(5):
        assert bucket.try_consume() is True


def test_bucket_rejects_once_exhausted():
    bucket = TokenBucket(capacity=2, refill_per_second=0.0)  # no refill within the test
    assert bucket.try_consume() is True
    assert bucket.try_consume() is True
    assert bucket.try_consume() is False


def test_bucket_refills_over_time():
    bucket = TokenBucket(capacity=1, refill_per_second=1000.0)  # fast refill for a quick test
    assert bucket.try_consume() is True
    assert bucket.try_consume() is False
    time.sleep(0.01)  # >= 1/1000s, enough for at least one token to refill
    assert bucket.try_consume() is True


def test_bucket_never_exceeds_capacity():
    bucket = TokenBucket(capacity=3, refill_per_second=1000.0)
    time.sleep(0.05)  # plenty of time to "over-refill" if capacity weren't enforced
    consumed = 0
    while bucket.try_consume():
        consumed += 1
    assert consumed == 3


def test_get_bucket_returns_same_instance_for_same_tool():
    reset_all_buckets()
    a = get_bucket("identity_get_user")
    b = get_bucket("identity_get_user")
    assert a is b


def test_get_bucket_returns_different_instances_per_tool():
    reset_all_buckets()
    a = get_bucket("identity_get_user")
    b = get_bucket("access_grant_access")
    assert a is not b


def test_check_rate_limit_allows_calls_within_capacity():
    reset_all_buckets()
    for _ in range(5):
        check_rate_limit("identity_get_user")  # must not raise


def test_check_rate_limit_raises_once_bucket_exhausted():
    reset_all_buckets()
    bucket = get_bucket("identity_disable_user")
    bucket.capacity = 1
    bucket._tokens = 1
    bucket.refill_per_second = 0.0
    check_rate_limit("identity_disable_user")
    try:
        check_rate_limit("identity_disable_user")
        raise AssertionError("expected RateLimitExceededError")
    except RateLimitExceededError as exc:
        assert "identity_disable_user" in str(exc)


def test_check_rate_limit_is_independent_per_tool():
    """Exhausting one tool's bucket must not affect a different tool's —
    a burst against get_user shouldn't also block disable_user."""
    reset_all_buckets()
    bucket = get_bucket("identity_get_user")
    bucket.capacity = 1
    bucket._tokens = 1
    bucket.refill_per_second = 0.0
    check_rate_limit("identity_get_user")
    try:
        check_rate_limit("identity_get_user")
        raise AssertionError("expected RateLimitExceededError")
    except RateLimitExceededError:
        pass
    check_rate_limit("identity_disable_user")  # unrelated tool, must not raise
