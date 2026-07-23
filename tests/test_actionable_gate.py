"""Tests for the entry-point actionability gate (check_actionable_node /
_looks_actionable / route_after_actionable_check) — a zero-LLM-call
fast-path for a ticket body with no real content, added after a live
observation: a ticket with body "Hi." correctly, but expensively,
round-tripped through classify + extract_username + plan (3 real LLM
calls) before concluding nothing was actionable. See _looks_actionable's
module docstring in app/agent/graph.py for the deliberate false-reject
bias (never reject a ticket with real, however terse, content).
"""

import json
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as graph_module
from app.agent.graph import (
    _looks_actionable,
    check_actionable_node,
    compile_graph,
    route_after_actionable_check,
)


# --- _looks_actionable: the pure heuristic ---------------------------------

def test_rejects_bare_greeting_body():
    assert not _looks_actionable("Subject: Onboard employee\n\nHi.")


def test_rejects_various_greeting_and_placeholder_bodies():
    for body in ["hello", "Hey.", "test", "Testing", "ping", "N/A", "none", "asdf"]:
        assert not _looks_actionable(f"Subject: X\n\n{body}"), f"expected rejection for {body!r}"


def test_rejects_empty_body():
    assert not _looks_actionable("Subject: Onboard employee\n\n")


def test_rejects_whitespace_only_body():
    assert not _looks_actionable("Subject: Onboard employee\n\n   \n  ")


def test_accepts_short_but_real_ticket():
    """The whole point: a genuinely actionable ticket must never be
    false-rejected just for being brief."""
    assert _looks_actionable("Subject: X\n\ndisable jsmith")


def test_accepts_realistic_onboarding_ticket():
    assert _looks_actionable(
        "Subject: Onboard employee\n\n"
        "Onboard Alex Doe (adoe), Engineering. Create account and default access."
    )


def test_greeting_check_is_whole_body_not_substring():
    """"Hi, please disable jsmith" must NOT be rejected — "hi" appearing
    as a substring/prefix is not the same as the body being JUST "hi"."""
    assert _looks_actionable("Subject: X\n\nHi, please disable jsmith's account.")


def test_looks_actionable_handles_text_with_no_subject_separator():
    """Ticket text missing the "Subject: ...\\n\\n" prefix (shouldn't
    happen via the real API, but must not crash) falls back to checking
    the whole string."""
    assert _looks_actionable("disable jsmith's account immediately please")
    assert not _looks_actionable("hi")


# --- check_actionable_node / route_after_actionable_check -----------------

def test_check_actionable_node_passes_through_real_ticket():
    state = {"ticket_text": "Subject: X\n\ndisable jsmith"}
    update = check_actionable_node(state)
    assert update == {}


def test_check_actionable_node_short_circuits_empty_ticket():
    state = {"ticket_text": "Subject: Onboard employee\n\nHi."}
    update = check_actionable_node(state)
    assert update == {"plan": [], "done": True}


def test_route_after_actionable_check_goes_to_classify_normally():
    assert route_after_actionable_check({"done": False}) == "classify"
    assert route_after_actionable_check({}) == "classify"


def test_route_after_actionable_check_goes_to_finalize_when_done():
    assert route_after_actionable_check({"done": True}) == "finalize"


# --- End-to-end: proves the fast path makes ZERO LLM calls -----------------

class _CountingLLM:
    """Any call at all is a failure for the fast-path test below — this
    ticket must never reach classify_node/plan_node."""

    def __init__(self):
        self.calls = 0

    async def ainvoke(self, messages):
        self.calls += 1
        raise AssertionError("LLM must not be called for a non-actionable ticket")


async def test_full_graph_skips_all_llm_calls_for_empty_ticket(monkeypatch):
    llm = _CountingLLM()
    monkeypatch.setattr(graph_module, "FallbackLLM", lambda: llm)

    graph = compile_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {
            "messages": [], "ticket_id": 999_101, "ticket_text": "Subject: Onboard employee\n\nHi.",
            "category": "", "plan": [], "plan_index": 0, "pending_approval_id": None,
            "results": [], "done": False, "error": None, "replan_count": 0, "tokens_used": 0,
        },
        config={"configurable": {"thread_id": "actionable-gate-test-1"}},
    )

    assert llm.calls == 0
    assert result["done"] is True
    assert result["plan"] == []
    assert result["results"] == []
    assert result["error"] is None


async def test_full_graph_still_plans_normally_for_a_real_ticket(monkeypatch):
    """Companion to the skip test above — same graph, a real ticket must
    still reach the planner exactly as before this gate existed."""
    from app.config import get_settings

    # Pin explicitly, same pattern as test_token_budget.py — this dev
    # machine's own .env may set SENSITIVE_ACTIONS to include
    # access_grant_access (it does, by the real app default), which would
    # gate this plan for approval instead of completing it; this test is
    # about proving the LLM call COUNT (3, unchanged), not exercising the
    # separate HITL gate.
    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()

    def _patch_mcp():
        @asynccontextmanager
        async def _session():
            class _FakeSession:
                pass

            yield _FakeSession()

        monkeypatch.setattr(graph_module, "mcp_session", _session)

        async def _call_tool(session, tool, args):
            return json.dumps({"username": args.get("username", "tuser"), "ok": True})

        monkeypatch.setattr(graph_module, "call_tool", _call_tool)

        async def _fake_discover_tool_reference():
            return "- access_grant_access(username, resource) -> test tool"

        monkeypatch.setattr(graph_module, "discover_tool_reference", _fake_discover_tool_reference)

    _patch_mcp()

    class _ScriptedLLM:
        def __init__(self, replies):
            self.replies = list(replies)
            self.calls = 0

        async def ainvoke(self, messages):
            class _Resp:
                content = self.replies[min(self.calls, len(self.replies) - 1)]
                usage_metadata = None

            self.calls += 1
            return _Resp()

    plan = json.dumps(
        [{"tool": "access_grant_access", "args": {"username": "tuser", "resource": "vpn"}, "reasoning": "r"}]
    )
    llm = _ScriptedLLM(["ACCESS_CHANGE", "tuser", plan])
    monkeypatch.setattr(graph_module, "FallbackLLM", lambda: llm)

    graph = compile_graph(checkpointer=InMemorySaver())
    result = await graph.ainvoke(
        {
            "messages": [], "ticket_id": 999_102, "ticket_text": "Subject: X\n\nGrant vpn to tuser",
            "category": "", "plan": [], "plan_index": 0, "pending_approval_id": None,
            "results": [], "done": False, "error": None, "replan_count": 0, "tokens_used": 0,
        },
        config={"configurable": {"thread_id": "actionable-gate-test-2"}},
    )

    get_settings.cache_clear()

    assert llm.calls == 3  # classify + extract_username + plan, unchanged
    assert result["done"] is True
    assert [r["ok"] for r in result["results"]] == [True]
