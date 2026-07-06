"""Integration tests proving the LangGraph node-level RetryPolicy actually
retries transient failures (not just that _is_transient_error classifies
them correctly in isolation — that's covered in test_graph_routing.py).

Uses a minimal single-node graph wrapping execute_step_node directly, rather
than the full compiled agent graph, so these tests exercise the retry
mechanics in isolation without needing to also drive plan_node (which
requires a real/fake LLM) just to reach execute_step.
"""

from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.graph import END, StateGraph

from app.agent import graph as graph_module
from app.agent.graph import AGENT_RETRY_POLICY, execute_step_node
from app.agent.state import AgentState


def _build_single_node_graph():
    g = StateGraph(AgentState)
    g.add_node("execute_step", execute_step_node, retry_policy=AGENT_RETRY_POLICY)
    g.set_entry_point("execute_step")
    g.add_edge("execute_step", END)
    return g.compile(checkpointer=InMemorySaver())


class _FakeSession:
    pass


def _make_flaky_mcp_session(fail_times: int, exc_factory=ConnectionError):
    """Returns an async context manager factory that raises exc_factory() the
    first `fail_times` calls, then yields a fake session successfully."""
    calls = {"count": 0}

    @asynccontextmanager
    async def _flaky_session():
        calls["count"] += 1
        if calls["count"] <= fail_times:
            raise exc_factory(f"transient failure #{calls['count']}")
        yield _FakeSession()

    return _flaky_session, calls


def _base_state(tool: str, args: dict, ticket_id: int) -> dict:
    return {
        "messages": [],
        "ticket_id": ticket_id,
        "ticket_text": "irrelevant",
        "plan": [{"tool": tool, "args": args, "reasoning": "test"}],
        "plan_index": 0,
        "pending_approval_id": None,
        "results": [],
        "done": False,
        "error": None,
    }


async def test_execute_step_retries_transient_failure_then_succeeds(monkeypatch):
    flaky_session, calls = _make_flaky_mcp_session(fail_times=2)
    monkeypatch.setattr(graph_module, "mcp_session", flaky_session)

    async def _fake_call_tool(session, tool, args):
        return '{"username": "tuser", "status": "active"}'

    monkeypatch.setattr(graph_module, "call_tool", _fake_call_tool)

    graph = _build_single_node_graph()
    state = _base_state("get_user", {"username": "tuser"}, ticket_id=1)
    config = {"configurable": {"thread_id": "retry-test-1"}}
    result = await graph.ainvoke(state, config=config)

    assert calls["count"] == 3, "expected 2 failures + 1 success = 3 total attempts"
    assert result["results"][0]["ok"] is True


async def test_execute_step_does_not_retry_permanent_tool_error(monkeypatch):
    """A ToolError-shaped failure (RuntimeError from call_tool) must fail
    once and be recorded as ok:False, not retried — retrying "user already
    disabled" would just waste attempts re-failing identically."""

    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    monkeypatch.setattr(graph_module, "mcp_session", _session)

    call_count = {"n": 0}

    async def _fake_call_tool(session, tool, args):
        call_count["n"] += 1
        raise RuntimeError("MCP tool 'disable_user' failed: User 'x' is already disabled")

    monkeypatch.setattr(graph_module, "call_tool", _fake_call_tool)

    graph = _build_single_node_graph()
    state = _base_state("disable_user", {"username": "x"}, ticket_id=2)
    config = {"configurable": {"thread_id": "retry-test-2"}}
    final_state = await graph.ainvoke(state, config=config)

    assert call_count["n"] == 1, "permanent tool errors must not be retried"
    assert final_state["results"][0]["ok"] is False
    assert "already disabled" in final_state["results"][0]["result"]


async def test_execute_step_gives_up_after_max_attempts(monkeypatch):
    """A persistently-failing transient error should exhaust the RetryPolicy's
    max_attempts and ultimately propagate as a graph-level failure, not
    retry forever."""
    import pytest

    flaky_session, calls = _make_flaky_mcp_session(fail_times=999)
    monkeypatch.setattr(graph_module, "mcp_session", flaky_session)

    async def _fake_call_tool(session, tool, args):
        return "unreachable"

    monkeypatch.setattr(graph_module, "call_tool", _fake_call_tool)

    graph = _build_single_node_graph()
    state = _base_state("get_user", {"username": "tuser"}, ticket_id=3)
    config = {"configurable": {"thread_id": "retry-test-3"}}

    with pytest.raises(ConnectionError):
        await graph.ainvoke(state, config=config)

    assert calls["count"] == AGENT_RETRY_POLICY.max_attempts
