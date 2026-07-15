"""End-to-end tests proving dynamic replanning (Stage 1.4) actually
re-invokes the planner and continues correctly after a stale-plan failure —
not just that route_after_execution picks the right branch in isolation
(covered in test_graph_routing.py)."""

from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as graph_module
from app.agent.graph import compile_graph


class _FakeSession:
    pass


def _patch_mcp(monkeypatch, tool_responses: dict):
    """tool_responses maps resource/tool key -> (ok, result_text). Each call
    consumes one entry from a per-key queue so a tool can be scripted to
    fail once then succeed on replan."""
    queues = {k: list(v) for k, v in tool_responses.items()}

    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    monkeypatch.setattr(graph_module, "mcp_session", _session)

    async def _call_tool(session, tool, args):
        key = args.get("resource") or args.get("username") or tool
        ok, text = queues[key].pop(0)
        if not ok:
            raise RuntimeError(f"MCP tool {tool!r} failed: {text}")
        return text

    monkeypatch.setattr(graph_module, "call_tool", _call_tool)

    # plan_node/replan_node call discover_tool_reference() (real MCP
    # tools/list) to build the prompt's tool reference — irrelevant to what
    # these tests actually exercise (replanning control flow), and
    # _FakeSession doesn't implement list_tools(). Stub it out rather than
    # extending the fake session, keeping these tests focused.
    async def _fake_discover_tool_reference():
        return "- identity_create_user(username, full_name, email) -> test tool"

    monkeypatch.setattr(graph_module, "discover_tool_reference", _fake_discover_tool_reference)


class _ScriptedLLM:
    """Returns each reply in sequence on successive ainvoke() calls —
    models a planner whose first plan turns out stale, then a replan call
    that accounts for what already happened."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    async def ainvoke(self, messages):
        class _Resp:
            def __init__(self, content):
                self.content = content

        reply = self.replies[min(self.calls, len(self.replies) - 1)]
        self.calls += 1
        return _Resp(reply)


async def test_replan_triggers_after_stale_plan_failure_and_completes(monkeypatch):
    """First plan step (create_user) fails because the user turned out to
    already exist (stale assumption — e.g. created by a concurrent ticket
    between planning and execution). Pins SENSITIVE_ACTIONS to its original
    disable/revoke-only set deliberately: create_user/grant_access joined
    the app's real default sensitive set after a security review, but a
    sensitive first step would route through await_approval and never reach
    execute_step without a real approval, which is a different concern
    (HITL) than what this test is verifying (replan-on-failure) — so this
    test pins its own policy rather than assume today's default forever.
    Replan should kick in, and the new plan (just a grant_access) should
    execute successfully."""
    import json

    from app.config import get_settings

    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()

    _patch_mcp(
        monkeypatch,
        {
            "tuser": [(False, "User already exists: 'tuser'")],  # create_user attempt
            "vpn": [(True, '{"username": "tuser", "access_grants": ["vpn"]}')],  # grant_access on replan
        },
    )

    scripted_llm = _ScriptedLLM(
        [
            json.dumps([{"tool": "grant_access", "args": {"username": "tuser", "resource": "vpn"}, "reasoning": "replanned"}]),
        ]
    )
    monkeypatch.setattr(graph_module, "FallbackLLM", lambda: scripted_llm)

    initial_plan = [
        {"tool": "create_user", "args": {"username": "tuser", "full_name": "T User", "email": "t@example.com"}, "reasoning": "onboard"}
    ]

    # classify_node and plan_node both normally call the LLM — patch both to
    # skip straight to the scenario under test (a stale-plan failure
    # triggering replan_node, which DOES need get_llm() scripted above).
    async def _passthrough_classify_node(state):
        return {"category": "ONBOARDING"}

    monkeypatch.setattr(graph_module, "classify_node", _passthrough_classify_node)

    async def _passthrough_plan_node(state):
        return {"plan": initial_plan, "plan_index": 0}

    monkeypatch.setattr(graph_module, "plan_node", _passthrough_plan_node)

    graph = compile_graph(checkpointer=InMemorySaver())
    state = {
        "messages": [], "ticket_id": 1, "ticket_text": "Onboard tuser",
        "plan": [], "plan_index": 0, "pending_approval_id": None,
        "results": [], "done": False, "error": None, "replan_count": 0,
    }
    config = {"configurable": {"thread_id": "replan-test-1"}}
    result = await graph.ainvoke(state, config=config)

    assert result["replan_count"] == 1, "expected exactly one replan to have occurred"
    assert result["done"] is True
    assert len(result["results"]) == 2, "the failed create_user + the successful replanned grant_access"
    assert result["results"][0]["ok"] is False
    assert result["results"][1]["ok"] is True
    assert result["results"][1]["tool"] == "grant_access"


async def test_replan_budget_prevents_infinite_loop(monkeypatch):
    """A pathological case where every replan attempt produces another
    stale-shaped failure must stop after MAX_REPLANS, not loop forever.

    Pins SENSITIVE_ACTIONS the same way as the test above — see its
    docstring for why create_user must stay non-sensitive here."""
    import json

    from app.agent.graph import MAX_REPLANS
    from app.config import get_settings

    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()

    call_count = {"n": 0}

    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    monkeypatch.setattr(graph_module, "mcp_session", _session)

    async def _always_fails(session, tool, args):
        call_count["n"] += 1
        raise RuntimeError("User already exists: 'tuser'")

    monkeypatch.setattr(graph_module, "call_tool", _always_fails)

    async def _fake_discover_tool_reference():
        return "- identity_create_user(username, full_name, email) -> test tool"

    monkeypatch.setattr(graph_module, "discover_tool_reference", _fake_discover_tool_reference)

    always_same_plan = json.dumps(
        [{"tool": "create_user", "args": {"username": "tuser", "full_name": "T", "email": "t@x.com"}, "reasoning": "retry"}]
    )
    scripted_llm = _ScriptedLLM([always_same_plan] * 10)
    monkeypatch.setattr(graph_module, "FallbackLLM", lambda: scripted_llm)

    initial_plan = [
        {"tool": "create_user", "args": {"username": "tuser", "full_name": "T", "email": "t@x.com"}, "reasoning": "onboard"}
    ]

    async def _passthrough_classify_node(state):
        return {"category": "ONBOARDING"}

    monkeypatch.setattr(graph_module, "classify_node", _passthrough_classify_node)

    async def _passthrough_plan_node(state):
        return {"plan": initial_plan, "plan_index": 0}

    monkeypatch.setattr(graph_module, "plan_node", _passthrough_plan_node)

    graph = compile_graph(checkpointer=InMemorySaver())
    state = {
        "messages": [], "ticket_id": 2, "ticket_text": "Onboard tuser",
        "plan": [], "plan_index": 0, "pending_approval_id": None,
        "results": [], "done": False, "error": None, "replan_count": 0,
    }
    config = {"configurable": {"thread_id": "replan-test-2"}}
    result = await graph.ainvoke(state, config=config)

    assert result["replan_count"] == MAX_REPLANS, "must stop replanning at the budget, not loop forever"
    assert result["done"] is True
