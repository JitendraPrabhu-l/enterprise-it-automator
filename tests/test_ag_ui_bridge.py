"""Tests for app/agent/ag_ui_bridge.py — the AG-UI protocol
(https://docs.ag-ui.com) event stream built on top of the same LangGraph
ticket run app/agent/runner.py drives for the plain JSON endpoints.

Two levels, mirroring tests/test_fanout.py's pattern for the graph itself:
  - Pure translation helpers (_state_delta_ops, _tool_events_for_delta,
    _interrupt_from_payload) tested directly, no graph involved.
  - stream_ticket_run/stream_resume_run tested against a REAL compiled
    graph (InMemorySaver, monkeypatched classify/plan nodes + call_tool —
    same fakes test_fanout.py uses) via app.agent.runner._get_graph
    monkeypatched to return it, proving the bridge's astream()-driven event
    sequence matches what the graph actually does, not just what the
    bridge's own code assumes it does.
"""

from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as graph_module
from app.agent import runner as runner_module
from app.agent.ag_ui_bridge import (
    _interrupt_from_payload,
    _state_delta_ops,
    _tool_events_for_delta,
    stream_resume_run,
    stream_ticket_run,
)
from app.agent.graph import compile_graph


def test_state_delta_ops_includes_plan_index_and_done():
    ops = _state_delta_ops("execute_step", {"plan_index": 2, "done": False})
    assert {"op": "add", "path": "/plan_index", "value": 2} in ops
    assert {"op": "add", "path": "/done", "value": False} in ops


def test_state_delta_ops_includes_category():
    ops = _state_delta_ops("classify", {"category": "OFFBOARDING"})
    assert ops == [{"op": "add", "path": "/category", "value": "OFFBOARDING"}]


def test_state_delta_ops_falls_back_to_last_node_when_delta_has_no_tracked_fields():
    ops = _state_delta_ops("route_step", {})
    assert ops == [{"op": "add", "path": "/last_node", "value": "route_step"}]


async def test_tool_events_for_delta_emits_start_then_result_per_step_result():
    delta = {
        "results": [
            {"tool": "grant_access", "args": {"username": "x"}, "result": '{"ok": true}', "ok": True},
        ]
    }
    events = await _tool_events_for_delta(delta)
    assert len(events) == 2
    assert events[0].type == "TOOL_CALL_START"
    assert events[0].tool_call_name == "grant_access"
    assert events[1].type == "TOOL_CALL_RESULT"
    assert events[1].tool_call_id == events[0].tool_call_id
    assert events[1].content == '{"ok": true}'


async def test_tool_events_for_delta_prefixes_error_content_on_failed_step():
    delta = {"results": [{"tool": "disable_user", "args": {}, "result": "no such user", "ok": False}]}
    events = await _tool_events_for_delta(delta)
    assert events[1].content == "ERROR: no such user"


async def test_tool_events_for_delta_empty_when_no_results_key():
    assert await _tool_events_for_delta({"plan_index": 1}) == []


def test_interrupt_from_payload_maps_fields_directly():
    payload = {
        "reason": "sensitive_action_requires_approval",
        "ticket_id": 5,
        "approval_id": 42,
        "tool": "disable_user",
        "args": {"username": "jsmith"},
        "agent_reasoning": "Employee is departing",
    }
    interrupt = _interrupt_from_payload(payload)
    assert interrupt.id == "42"
    assert interrupt.reason == "sensitive_action_requires_approval"
    assert interrupt.tool_call_id == "disable_user"
    assert "disable_user" in interrupt.message
    assert "Employee is departing" in interrupt.message
    assert interrupt.metadata == {
        "ticket_id": 5,
        "approval_id": 42,
        "tool": "disable_user",
        "args": {"username": "jsmith"},
    }


class _FakeSession:
    pass


def _patch_call_tool(monkeypatch):
    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    monkeypatch.setattr(graph_module, "mcp_session", _session)

    async def _fake_call_tool(session, tool, args):
        return f'{{"username": "{args.get("username")}", "resource": "{args.get("resource")}"}}'

    monkeypatch.setattr(graph_module, "call_tool", _fake_call_tool)


def _patch_plan_node_passthrough(monkeypatch, plan: list):
    async def _passthrough_classify_node(state):
        return {"category": "ACCESS_CHANGE"}

    monkeypatch.setattr(graph_module, "classify_node", _passthrough_classify_node)

    async def _passthrough_plan_node(state):
        return {"plan": plan, "plan_index": 0}

    monkeypatch.setattr(graph_module, "plan_node", _passthrough_plan_node)


async def test_stream_ticket_run_emits_run_started_then_run_finished_success(monkeypatch):
    _patch_call_tool(monkeypatch)
    _patch_plan_node_passthrough(
        monkeypatch,
        [{"tool": "grant_access", "args": {"username": "x", "resource": "vpn"}, "reasoning": "r"}],
    )
    graph = compile_graph(checkpointer=InMemorySaver())
    monkeypatch.setattr(runner_module, "_get_graph", lambda: _async_return(graph))

    events = [e async for e in stream_ticket_run(ticket_id=101, ticket_text="grant vpn to x", run_id="run-1")]

    assert events[0].type == "RUN_STARTED"
    assert events[0].thread_id == "ticket-101"
    assert events[-1].type == "RUN_FINISHED"
    assert events[-1].outcome.type == "success"


async def test_stream_ticket_run_emits_tool_call_events_for_executed_step(monkeypatch):
    _patch_call_tool(monkeypatch)
    _patch_plan_node_passthrough(
        monkeypatch,
        [{"tool": "grant_access", "args": {"username": "x", "resource": "vpn"}, "reasoning": "r"}],
    )
    graph = compile_graph(checkpointer=InMemorySaver())
    monkeypatch.setattr(runner_module, "_get_graph", lambda: _async_return(graph))

    events = [e async for e in stream_ticket_run(ticket_id=102, ticket_text="grant vpn to x", run_id="run-2")]

    tool_starts = [e for e in events if e.type == "TOOL_CALL_START"]
    tool_results = [e for e in events if e.type == "TOOL_CALL_RESULT"]
    assert len(tool_starts) == 1
    assert tool_starts[0].tool_call_name == "grant_access"
    assert len(tool_results) == 1


async def test_stream_ticket_run_emits_step_started_and_finished_pairs(monkeypatch):
    _patch_call_tool(monkeypatch)
    _patch_plan_node_passthrough(monkeypatch, [])
    graph = compile_graph(checkpointer=InMemorySaver())
    monkeypatch.setattr(runner_module, "_get_graph", lambda: _async_return(graph))

    events = [e async for e in stream_ticket_run(ticket_id=103, ticket_text="no-op ticket", run_id="run-3")]

    step_starts = [e.step_name for e in events if e.type == "STEP_STARTED"]
    step_finishes = [e.step_name for e in events if e.type == "STEP_FINISHED"]
    assert step_starts == step_finishes
    assert "classify" in step_starts
    assert "finalize" in step_starts


async def test_stream_ticket_run_emits_interrupt_outcome_for_sensitive_step(monkeypatch, tmp_path):
    from app.config import get_settings
    from app.db import session as db_session_module

    db_path = tmp_path / "ag_ui_bridge_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    try:
        await db_session_module.init_db()
        from app.db.models import Ticket, TicketStatus

        async with db_session_module.session_scope() as s:
            s.add(Ticket(id=104, requester="hr@example.com", subject="t", body="t", status=TicketStatus.PLANNING))

        _patch_call_tool(monkeypatch)
        _patch_plan_node_passthrough(
            monkeypatch,
            [{"tool": "disable_user", "args": {"username": "x"}, "reasoning": "departing employee"}],
        )
        graph = compile_graph(checkpointer=InMemorySaver())
        monkeypatch.setattr(runner_module, "_get_graph", lambda: _async_return(graph))

        events = [e async for e in stream_ticket_run(ticket_id=104, ticket_text="offboard x", run_id="run-4")]

        assert events[-1].type == "RUN_FINISHED"
        assert events[-1].outcome.type == "interrupt"
        interrupt = events[-1].outcome.interrupts[0]
        assert interrupt.tool_call_id == "disable_user"
        assert "departing employee" in interrupt.message
        # No TOOL_CALL_* for the gated step — it must not execute before approval.
        assert not [e for e in events if e.type == "TOOL_CALL_START"]
    finally:
        db_session_module._engine = None
        db_session_module._session_factory = None
        get_settings.cache_clear()


async def test_stream_ticket_run_emits_run_error_on_unhandled_exception(monkeypatch):
    async def _broken_classify_node(state):
        raise RuntimeError("boom")

    monkeypatch.setattr(graph_module, "classify_node", _broken_classify_node)
    graph = compile_graph(checkpointer=InMemorySaver())
    monkeypatch.setattr(runner_module, "_get_graph", lambda: _async_return(graph))

    events = [e async for e in stream_ticket_run(ticket_id=105, ticket_text="irrelevant", run_id="run-5")]

    assert events[0].type == "RUN_STARTED"
    assert events[-1].type == "RUN_ERROR"
    assert "boom" in events[-1].message


async def test_stream_resume_run_resumes_from_checkpointed_interrupt(monkeypatch, tmp_path):
    """A resume must pick up from exactly where the earlier stream_ticket_run
    call paused (same checkpointer, same thread_id) and complete the
    previously-gated step once approved — proving the two entry points
    share one underlying run rather than each starting fresh."""
    from app.config import get_settings
    from app.db import session as db_session_module

    db_path = tmp_path / "ag_ui_bridge_resume_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    try:
        await db_session_module.init_db()
        from app.db.models import Approval, ApprovalStatus, Ticket, TicketStatus

        async with db_session_module.session_scope() as s:
            s.add(Ticket(id=106, requester="hr@example.com", subject="t", body="t", status=TicketStatus.PLANNING))

        _patch_call_tool(monkeypatch)
        _patch_plan_node_passthrough(
            monkeypatch,
            [{"tool": "disable_user", "args": {"username": "x"}, "reasoning": "departing employee"}],
        )
        graph = compile_graph(checkpointer=InMemorySaver())
        monkeypatch.setattr(runner_module, "_get_graph", lambda: _async_return(graph))

        first_events = [
            e async for e in stream_ticket_run(ticket_id=106, ticket_text="offboard x", run_id="run-6a")
        ]
        assert first_events[-1].outcome.type == "interrupt"
        approval_id = int(first_events[-1].outcome.interrupts[0].id)

        async with db_session_module.session_scope() as s:
            approval = await s.get(Approval, approval_id)
            approval.status = ApprovalStatus.APPROVED

        resumed_events = [e async for e in stream_resume_run(ticket_id=106, run_id="run-6b")]

        assert resumed_events[0].type == "RUN_STARTED"
        tool_starts = [e for e in resumed_events if e.type == "TOOL_CALL_START"]
        assert len(tool_starts) == 1
        assert tool_starts[0].tool_call_name == "disable_user"
        assert resumed_events[-1].type == "RUN_FINISHED"
        assert resumed_events[-1].outcome.type == "success"
    finally:
        db_session_module._engine = None
        db_session_module._session_factory = None
        get_settings.cache_clear()


async def _async_return(value):
    return value
