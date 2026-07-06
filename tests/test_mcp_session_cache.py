"""Tests for the per-ticket-run MCP session cache (Stage 1.5).

The owner-task/queue-proxy design exists specifically because a naive
"cache a session object, let any task use it" approach broke a real anyio
constraint live: MCP's stdio transport ties its task group to whichever
task opens it, and LangGraph runs nodes (including concurrent Send() fan-out
branches) as separate tasks from runner.py's start_ticket_run. These tests
exercise the proxy's request/response semantics and, critically, prove a
session opened in one task can be used correctly from OTHER tasks — the
exact scenario that broke the earlier design.
"""

import asyncio
from contextlib import asynccontextmanager

from app.agent import mcp_session_cache as cache_module


class _FakeSession:
    pass


def _patch_fake_mcp_session(monkeypatch, call_log: list, responses: dict | None = None):
    @asynccontextmanager
    async def _fake_mcp_session():
        yield _FakeSession()

    monkeypatch.setattr(cache_module, "mcp_session", _fake_mcp_session)

    async def _fake_call_tool(session, tool, args):
        call_log.append((tool, args))
        if responses and tool in responses:
            return responses[tool]
        return f"{tool}-result"

    monkeypatch.setattr(cache_module, "_call_tool", _fake_call_tool)


async def test_get_cached_proxy_returns_none_when_no_run_active():
    assert cache_module.get_cached_proxy(999) is None


async def test_proxy_accessible_via_get_cached_proxy_during_run(monkeypatch):
    call_log = []
    _patch_fake_mcp_session(monkeypatch, call_log)

    async with cache_module.ticket_run_session(1):
        proxy = cache_module.get_cached_proxy(1)
        assert proxy is not None
        assert isinstance(proxy, cache_module.SessionProxy)


async def test_proxy_removed_after_ticket_run_session_exits(monkeypatch):
    call_log = []
    _patch_fake_mcp_session(monkeypatch, call_log)

    async with cache_module.ticket_run_session(2):
        assert cache_module.get_cached_proxy(2) is not None

    assert cache_module.get_cached_proxy(2) is None, "proxy must be cleaned up once the run's context exits"


async def test_call_tool_routes_through_owner_and_returns_result(monkeypatch):
    call_log = []
    _patch_fake_mcp_session(monkeypatch, call_log, responses={"get_user": "user-data"})

    async with cache_module.ticket_run_session(3) as proxy:
        result = await proxy.call_tool("get_user", {"username": "x"})
        assert result == "user-data"
        assert call_log == [("get_user", {"username": "x"})]


async def test_multiple_calls_reuse_the_same_underlying_session(monkeypatch):
    session_open_count = {"n": 0}

    @asynccontextmanager
    async def _counting_mcp_session():
        session_open_count["n"] += 1
        yield _FakeSession()

    monkeypatch.setattr(cache_module, "mcp_session", _counting_mcp_session)

    call_log = []

    async def _fake_call_tool(session, tool, args):
        call_log.append(tool)
        return f"{tool}-ok"

    monkeypatch.setattr(cache_module, "_call_tool", _fake_call_tool)

    async with cache_module.ticket_run_session(4) as proxy:
        await proxy.call_tool("get_user", {})
        await proxy.call_tool("grant_access", {})
        await proxy.call_tool("grant_access", {})

    assert session_open_count["n"] == 1, "must open exactly one session for the whole ticket run"
    assert call_log == ["get_user", "grant_access", "grant_access"]


async def test_calls_from_a_different_task_than_the_owner_succeed(monkeypatch):
    """The exact scenario that broke the earlier lazy-shared-session design:
    a call_tool() invoked from a task OTHER than the one running
    ticket_run_session's caller (simulating a LangGraph node's own task, or
    a concurrent Send() fan-out branch). Must succeed cleanly, proving the
    owner-task/queue design actually solves the cross-task problem rather
    than just hiding it.
    """
    call_log = []
    _patch_fake_mcp_session(monkeypatch, call_log, responses={"grant_access": "granted"})

    async def call_from_separate_task(proxy):
        return await proxy.call_tool("grant_access", {"resource": "vpn"})

    async with cache_module.ticket_run_session(5) as proxy:
        result = await asyncio.create_task(call_from_separate_task(proxy))
        assert result == "granted"


async def test_concurrent_calls_from_multiple_tasks_all_complete(monkeypatch):
    """Simulates parallel fan-out: several tasks call_tool() concurrently
    through the same proxy. All must complete with their own correct
    result, not get mixed up with each other's."""
    call_log = []

    @asynccontextmanager
    async def _fake_mcp_session():
        yield _FakeSession()

    monkeypatch.setattr(cache_module, "mcp_session", _fake_mcp_session)

    async def _fake_call_tool(session, tool, args):
        call_log.append(args["resource"])
        await asyncio.sleep(0.01)
        return f"granted-{args['resource']}"

    monkeypatch.setattr(cache_module, "_call_tool", _fake_call_tool)

    async with cache_module.ticket_run_session(6) as proxy:
        results = await asyncio.gather(
            *[proxy.call_tool("grant_access", {"resource": r}) for r in ["vpn", "github", "jira"]]
        )

    assert set(results) == {"granted-vpn", "granted-github", "granted-jira"}
    assert set(call_log) == {"vpn", "github", "jira"}


async def test_session_closes_cleanly_on_normal_exit(monkeypatch):
    closed = {"value": False}

    @asynccontextmanager
    async def _fake_mcp_session():
        try:
            yield _FakeSession()
        finally:
            closed["value"] = True

    monkeypatch.setattr(cache_module, "mcp_session", _fake_mcp_session)

    async def _fake_call_tool(session, tool, args):
        return "ok"

    monkeypatch.setattr(cache_module, "_call_tool", _fake_call_tool)

    async with cache_module.ticket_run_session(7) as proxy:
        await proxy.call_tool("get_user", {})
        assert closed["value"] is False

    assert closed["value"] is True, "the underlying mcp_session() context must close when the run exits"


async def test_session_closes_even_when_caller_raises(monkeypatch):
    closed = {"value": False}

    @asynccontextmanager
    async def _fake_mcp_session():
        try:
            yield _FakeSession()
        finally:
            closed["value"] = True

    monkeypatch.setattr(cache_module, "mcp_session", _fake_mcp_session)

    try:
        async with cache_module.ticket_run_session(8):
            raise ValueError("simulated failure mid-run")
    except ValueError:
        pass

    assert closed["value"] is True, "session must close even if the graph run raises"
    assert cache_module.get_cached_proxy(8) is None


async def test_tool_call_exception_propagates_to_caller_not_owner_task(monkeypatch):
    """If the tool call itself raises (e.g. a ToolError crossing the MCP
    wire as a RuntimeError), the exception must reach the calling task via
    the future — not crash the owner task and silently hang other callers.
    """
    import pytest

    @asynccontextmanager
    async def _fake_mcp_session():
        yield _FakeSession()

    monkeypatch.setattr(cache_module, "mcp_session", _fake_mcp_session)

    async def _failing_call_tool(session, tool, args):
        raise RuntimeError("MCP tool 'disable_user' failed: already disabled")

    monkeypatch.setattr(cache_module, "_call_tool", _failing_call_tool)

    async with cache_module.ticket_run_session(9) as proxy:
        with pytest.raises(RuntimeError, match="already disabled"):
            await proxy.call_tool("disable_user", {"username": "x"})

        # owner task must still be alive and serving further requests
        # after one request failed — prove it with a second, successful call
        async def _ok_after_failure(session, tool, args):
            return "still-alive"

        monkeypatch.setattr(cache_module, "_call_tool", _ok_after_failure)
        result = await proxy.call_tool("get_user", {"username": "y"})
        assert result == "still-alive"


async def test_setup_failure_surfaces_immediately_not_on_first_call(monkeypatch):
    """If the MCP session itself fails to open (e.g. subprocess couldn't
    start), ticket_run_session() must raise right away rather than
    returning a proxy whose first call_tool() would hang forever waiting
    on an owner task that already exited."""
    import pytest

    @asynccontextmanager
    async def _failing_mcp_session():
        raise ConnectionError("subprocess failed to start")
        yield  # pragma: no cover

    monkeypatch.setattr(cache_module, "mcp_session", _failing_mcp_session)

    with pytest.raises(ConnectionError, match="subprocess failed to start"):
        async with cache_module.ticket_run_session(10):
            pass  # pragma: no cover
