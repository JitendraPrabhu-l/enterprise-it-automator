"""End-to-end tests proving the parallel fan-out (Stage 1.3) actually
executes concurrently and produces correct, complete, correctly-ordered
results — not just that route_after_step_check emits the right Send list
(that's covered in test_graph_routing.py)."""

import asyncio
import time
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as graph_module
from app.agent.graph import compile_graph


class _FakeSession:
    pass


def _patch_slow_call_tool(monkeypatch, delay_seconds: float, call_log: list):
    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    monkeypatch.setattr(graph_module, "mcp_session", _session)

    async def _slow_call_tool(session, tool, args):
        call_log.append((tool, args.get("resource")))
        await asyncio.sleep(delay_seconds)
        return f'{{"username": "{args.get("username")}", "resource": "{args.get("resource")}"}}'

    monkeypatch.setattr(graph_module, "call_tool", _slow_call_tool)


def _patch_plan_node_passthrough(monkeypatch, plan: list):
    """classify_node (the graph's entry point) and plan_node would normally
    each call the LLM — classify_node to pick a category, plan_node to
    produce the plan. These tests care about routing/execution AFTER
    planning, not classification or planning themselves, so replace both
    with fast passthroughs — exercising the real graph wiring
    (route_after_step_check, Send fan-out, join_batch, await_approval)
    rather than a hand-built mini-graph or paying for 2 real LLM round trips
    per test.
    """

    async def _passthrough_classify_node(state):
        return {"category": "ACCESS_CHANGE"}

    monkeypatch.setattr(graph_module, "classify_node", _passthrough_classify_node)

    async def _passthrough_plan_node(state):
        return {"plan": plan, "plan_index": 0}

    monkeypatch.setattr(graph_module, "plan_node", _passthrough_plan_node)


async def test_fanout_batch_runs_concurrently_not_sequentially(monkeypatch):
    """3 steps at 0.3s each: sequential would take ~0.9s+, parallel should
    take close to 0.3s. A generous 0.7s ceiling leaves margin for test-env
    overhead while still failing decisively if execution were sequential.

    Pins SENSITIVE_ACTIONS to its original disable/revoke-only set —
    grant_access was added to the app's real default sensitive set after a
    security review (create_user/grant_access now require approval, same as
    disable/revoke), but this test is about fan-out MECHANICS, not about
    grant_access specifically, so it shouldn't be coupled to that policy
    changing again in the future.
    """
    from app.config import get_settings

    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()
    call_log = []
    _patch_slow_call_tool(monkeypatch, delay_seconds=0.3, call_log=call_log)

    plan = [
        {"tool": "grant_access", "args": {"username": "x", "resource": r}, "reasoning": "r"}
        for r in ["vpn", "github", "jira"]
    ]
    _patch_plan_node_passthrough(monkeypatch, plan)
    graph = compile_graph(checkpointer=InMemorySaver())
    state = {
        "messages": [], "ticket_id": 1, "ticket_text": "irrelevant",
        "plan": [], "plan_index": 0, "pending_approval_id": None,
        "results": [], "done": False, "error": None,
    }
    config = {"configurable": {"thread_id": "fanout-timing-1"}}

    start = time.monotonic()
    result = await graph.ainvoke(state, config=config)
    elapsed = time.monotonic() - start

    assert elapsed < 0.7, f"expected parallel execution (~0.3s), took {elapsed:.2f}s — looks sequential"
    assert len(call_log) == 3
    assert len(result["results"]) == 3
    assert all(r["ok"] for r in result["results"])
    assert result["plan_index"] == 3
    assert result["done"] is True


async def test_fanout_batch_all_results_present_and_correct(monkeypatch):
    """Pins SENSITIVE_ACTIONS the same way as the timing test above — see
    its docstring for why."""
    from app.config import get_settings

    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()
    call_log = []
    _patch_slow_call_tool(monkeypatch, delay_seconds=0.0, call_log=call_log)

    plan = [
        {"tool": "grant_access", "args": {"username": "x", "resource": r}, "reasoning": "r"}
        for r in ["vpn", "github", "jira", "salesforce", "admin-panel"]
    ]
    _patch_plan_node_passthrough(monkeypatch, plan)
    graph = compile_graph(checkpointer=InMemorySaver())
    state = {
        "messages": [], "ticket_id": 2, "ticket_text": "irrelevant",
        "plan": [], "plan_index": 0, "pending_approval_id": None,
        "results": [], "done": False, "error": None,
    }
    config = {"configurable": {"thread_id": "fanout-completeness-1"}}
    result = await graph.ainvoke(state, config=config)

    assert len(result["results"]) == 5
    resources_seen = {r["args"]["resource"] for r in result["results"]}
    assert resources_seen == {"vpn", "github", "jira", "salesforce", "admin-panel"}


async def test_mixed_plan_batches_non_sensitive_then_gates_sensitive_step(monkeypatch, tmp_path):
    """A plan with 2 non-sensitive steps followed by a sensitive one: the
    first two should fan out together, then the graph must still correctly
    pause at await_approval for the sensitive step — proving fan-out doesn't
    bypass the HITL security boundary.

    await_approval_node writes a real Approval row via app.db.session's
    module-level engine/session-factory singletons, which are cached on
    first use and ignore later DATABASE_URL changes — so this test must
    reset those globals to point at an isolated on-disk DB, or it would
    silently write into whatever DB a previous test/run already initialized
    (verified this the hard way: an earlier version of this test wrote a
    stray Approval row into the real dev database).

    Also pins SENSITIVE_ACTIONS to disable/revoke-only — see the timing
    test above's docstring for why grant_access must NOT be treated as
    sensitive here even though it now is in the app's real default config.
    """
    from app.config import get_settings
    from app.db import session as db_session_module

    db_path = tmp_path / "fanout_test.db"
    monkeypatch.setenv("DATABASE_URL", f"sqlite+aiosqlite:///{db_path.as_posix()}")
    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()
    db_session_module._engine = None
    db_session_module._session_factory = None
    try:
        await db_session_module.init_db()

        from app.db.models import Ticket, TicketStatus

        async with db_session_module.session_scope() as s:
            s.add(Ticket(id=3, requester="hr@example.com", subject="test", body="test", status=TicketStatus.PLANNING))

        call_log = []
        _patch_slow_call_tool(monkeypatch, delay_seconds=0.0, call_log=call_log)

        plan = [
            {"tool": "grant_access", "args": {"username": "x", "resource": "vpn"}, "reasoning": "r1"},
            {"tool": "grant_access", "args": {"username": "x", "resource": "github"}, "reasoning": "r2"},
            {"tool": "disable_user", "args": {"username": "x"}, "reasoning": "r3"},
        ]
        _patch_plan_node_passthrough(monkeypatch, plan)
        graph = compile_graph(checkpointer=InMemorySaver())
        state = {
            "messages": [], "ticket_id": 3, "ticket_text": "irrelevant",
            "plan": [], "plan_index": 0, "pending_approval_id": None,
            "results": [], "done": False, "error": None,
        }
        config = {"configurable": {"thread_id": "fanout-mixed-1"}}
        result = await graph.ainvoke(state, config=config)

        snapshot = await graph.aget_state(config)
        assert bool(snapshot.interrupts), "expected the graph to pause at the sensitive disable_user step"
        assert len(result["results"]) == 2, "only the 2 batched non-sensitive steps should have executed so far"
        assert {r["args"]["resource"] for r in result["results"]} == {"vpn", "github"}
        assert result["done"] is False, "must not be done — still waiting on HITL approval"
    finally:
        db_session_module._engine = None
        db_session_module._session_factory = None
        get_settings.cache_clear()
