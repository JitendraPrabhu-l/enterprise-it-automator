"""Integration tests proving the circuit breaker actually gates
mcp_client.call_tool() — not just that the breaker's state machine works in
isolation (covered in test_circuit_breaker.py)."""

from unittest.mock import AsyncMock

import pytest

from app.agent.mcp_client import call_tool
from app.mcp_server.circuit_breaker import CircuitOpenError, CircuitState, get_breaker, reset_all_breakers


def _make_error_result(text: str):
    class _Block:
        def __init__(self, t):
            self.text = t

    class _Result:
        isError = True
        content = [_Block(text)]

    return _Result()


def _make_success_result(text: str):
    class _Block:
        def __init__(self, t):
            self.text = t

    class _Result:
        isError = False
        content = [_Block(text)]

    return _Result()


async def test_transient_failures_trip_the_breaker_for_that_domain():
    reset_all_breakers()
    session = AsyncMock()
    # transport-level exception (not a ToolError-shaped result) — transient
    session.call_tool.side_effect = ConnectionError("connection refused")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await call_tool(session, "identity_get_user", {"username": "x"})

    breaker = get_breaker("identity")
    assert breaker.state == CircuitState.OPEN


async def test_open_breaker_rejects_further_calls_without_hitting_transport():
    reset_all_breakers()
    session = AsyncMock()
    session.call_tool.side_effect = ConnectionError("connection refused")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await call_tool(session, "identity_get_user", {"username": "x"})

    session.call_tool.reset_mock()
    with pytest.raises(CircuitOpenError):
        await call_tool(session, "identity_get_user", {"username": "y"})

    assert session.call_tool.call_count == 0, "must fail fast without calling the transport at all"


async def test_application_logic_errors_do_not_trip_the_breaker():
    """A ToolError-shaped failure (e.g. 'user already exists') is not a
    backend health problem — repeated calls that each legitimately fail for
    business reasons must not trip the circuit breaker."""
    reset_all_breakers()
    session = AsyncMock()
    session.call_tool.return_value = _make_error_result("User already exists: 'x'")

    for _ in range(5):
        with pytest.raises(RuntimeError, match="already exists"):
            await call_tool(session, "identity_create_user", {"username": "x"})

    breaker = get_breaker("identity")
    assert breaker.state == CircuitState.CLOSED


async def test_success_keeps_breaker_closed():
    reset_all_breakers()
    session = AsyncMock()
    session.call_tool.return_value = _make_success_result("ok")

    for _ in range(5):
        result = await call_tool(session, "access_grant_access", {"username": "x", "resource": "vpn"})
        assert result == "ok"

    breaker = get_breaker("access")
    assert breaker.state == CircuitState.CLOSED


async def test_different_domains_have_independent_breakers_via_call_tool():
    reset_all_breakers()
    session = AsyncMock()
    session.call_tool.side_effect = ConnectionError("connection refused")

    for _ in range(3):
        with pytest.raises(ConnectionError):
            await call_tool(session, "ticketing_add_ticket_comment", {"ticket_id": 1, "comment": "x"})

    assert get_breaker("ticketing").state == CircuitState.OPEN
    assert get_breaker("identity").state == CircuitState.CLOSED

    # identity domain calls should still go through normally
    session.call_tool.side_effect = None
    session.call_tool.return_value = _make_success_result("still works")
    result = await call_tool(session, "identity_get_user", {"username": "x"})
    assert result == "still works"
