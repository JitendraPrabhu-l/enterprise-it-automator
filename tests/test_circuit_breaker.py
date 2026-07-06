"""Tests for the per-domain circuit breaker (Stage 2.4)."""

import time

from app.mcp_server.circuit_breaker import CircuitBreaker, CircuitState, get_breaker, reset_all_breakers


def test_starts_closed():
    cb = CircuitBreaker()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_stays_closed_under_threshold():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_opens_after_threshold_failures():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


def test_success_resets_failure_count():
    cb = CircuitBreaker(failure_threshold=3)
    cb.record_failure()
    cb.record_failure()
    cb.record_success()
    cb.record_failure()
    cb.record_failure()
    assert cb.state == CircuitState.CLOSED, "2 failures after a reset must not trip a threshold-3 breaker"


def test_transitions_to_half_open_after_recovery_timeout():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False

    time.sleep(0.06)
    assert cb.allow_request() is True
    assert cb.state == CircuitState.HALF_OPEN


def test_half_open_success_closes_circuit():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    cb.record_failure()
    time.sleep(0.06)
    cb.allow_request()  # transitions to HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_success()
    assert cb.state == CircuitState.CLOSED
    assert cb.allow_request() is True


def test_half_open_failure_reopens_circuit():
    cb = CircuitBreaker(failure_threshold=1, recovery_timeout_seconds=0.05)
    cb.record_failure()
    time.sleep(0.06)
    cb.allow_request()  # transitions to HALF_OPEN
    assert cb.state == CircuitState.HALF_OPEN

    cb.record_failure()
    assert cb.state == CircuitState.OPEN
    assert cb.allow_request() is False


def test_get_breaker_returns_same_instance_for_same_domain():
    reset_all_breakers()
    b1 = get_breaker("identity")
    b2 = get_breaker("identity")
    assert b1 is b2


def test_get_breaker_returns_different_instances_for_different_domains():
    reset_all_breakers()
    identity_breaker = get_breaker("identity")
    access_breaker = get_breaker("access")
    assert identity_breaker is not access_breaker


def test_one_domains_failures_do_not_affect_another_domain():
    reset_all_breakers()
    identity_breaker = get_breaker("identity")
    ticketing_breaker = get_breaker("ticketing")

    for _ in range(5):
        ticketing_breaker.record_failure()

    assert ticketing_breaker.state == CircuitState.OPEN
    assert identity_breaker.state == CircuitState.CLOSED
    assert identity_breaker.allow_request() is True
