"""Tests for the per-ticket LLM token budget (app/agent/token_budget.py and
its enforcement in plan_node/replan_node).

The end-to-end cases drive a real compiled graph with a scripted LLM whose
responses carry usage_metadata — the same pattern as test_replanning.py,
plus token accounting. record_llm_call is the production choke point that
feeds the accumulator, so these tests exercise the real wiring, not a
hand-fed counter.
"""

import json
from contextlib import asynccontextmanager

from langgraph.checkpoint.memory import InMemorySaver
from prometheus_client import REGISTRY

from app.agent import graph as graph_module
from app.agent import token_budget
from app.agent.graph import compile_graph
from app.config import get_settings


def _sample(name: str, labels: dict | None = None) -> float:
    return REGISTRY.get_sample_value(name, labels or {}) or 0.0


class _FakeSession:
    pass


def _patch_mcp(monkeypatch):
    @asynccontextmanager
    async def _session():
        yield _FakeSession()

    monkeypatch.setattr(graph_module, "mcp_session", _session)

    async def _call_tool(session, tool, args):
        return json.dumps({"username": args.get("username", "tuser"), "ok": True})

    monkeypatch.setattr(graph_module, "call_tool", _call_tool)

    async def _fake_discover_tool_reference():
        return "- access_grant_access(username, resource) -> test tool"

    monkeypatch.setattr(graph_module, "discover_tool_reference", _fake_discover_tool_reference)


class _MeteredLLM:
    """Scripted replies, each reporting fixed token usage — what a real
    LangChain chat model surfaces via usage_metadata."""

    def __init__(self, replies: list[str], tokens_per_call: int = 40):
        self.replies = list(replies)
        self.tokens_per_call = tokens_per_call
        self.calls = 0

    async def ainvoke(self, messages):
        per_direction = self.tokens_per_call // 2

        class _Resp:
            content = self.replies[min(self.calls, len(self.replies) - 1)]
            usage_metadata = {
                "input_tokens": per_direction,
                "output_tokens": self.tokens_per_call - per_direction,
            }

        self.calls += 1
        return _Resp()


def test_accounting_flows_through_record_llm_call():
    from app.observability import record_llm_call

    class _Resp:
        usage_metadata = {"input_tokens": 30, "output_tokens": 12}

    token_budget.start_accounting(0)
    record_llm_call("plan", "test-model", _Resp())
    assert token_budget.current_total() == 42

    # Seeded accounting (the resume path) keeps prior spend.
    token_budget.start_accounting(100)
    record_llm_call("plan", "test-model", _Resp())
    assert token_budget.current_total() == 142


def test_budget_disabled_by_default(monkeypatch):
    monkeypatch.delenv("MAX_TOKENS_PER_TICKET", raising=False)
    get_settings.cache_clear()
    try:
        token_budget.start_accounting(10_000_000)
        assert token_budget.budget_exceeded() is False
    finally:
        get_settings.cache_clear()


def test_budget_exceeded_when_limit_set(monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_TICKET", "100")
    get_settings.cache_clear()
    try:
        token_budget.start_accounting(99)
        assert token_budget.budget_exceeded() is False
        token_budget.add_tokens(1)
        assert token_budget.budget_exceeded() is True
    finally:
        get_settings.cache_clear()


async def test_graph_fails_ticket_when_budget_spent(monkeypatch):
    """classify consumes the whole budget; plan_node must then refuse to
    invoke the planner and route the ticket to FAILED via finalize, with the
    budget counter incremented and the spend recorded in state.
    """
    monkeypatch.setenv("MAX_TOKENS_PER_TICKET", "50")
    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()
    _patch_mcp(monkeypatch)

    # One classify call at 60 tokens -> budget (50) already spent by plan time.
    llm = _MeteredLLM(replies=["ACCESS_CHANGE"], tokens_per_call=60)
    monkeypatch.setattr(graph_module, "FallbackLLM", lambda: llm)

    graph = compile_graph(checkpointer=InMemorySaver())
    token_budget.start_accounting(0)
    result = await graph.ainvoke(
        {
            "messages": [], "ticket_id": 999_001, "ticket_text": "Grant vpn to tuser",
            "category": "", "plan": [], "plan_index": 0, "pending_approval_id": None,
            "results": [], "done": False, "error": None, "replan_count": 0, "tokens_used": 0,
        },
        config={"configurable": {"thread_id": "budget-test-1"}},
    )
    get_settings.cache_clear()

    assert result["done"] is True
    assert "token budget" in (result["error"] or "")
    assert result["tokens_used"] >= 50
    # Exactly one LLM call happened (classify) — the planner was never paid for.
    assert llm.calls == 1


async def test_graph_completes_normally_when_budget_disabled(monkeypatch):
    """Same scripted run with no budget configured — must complete exactly
    as before the budget existed (the don't-break-anything case).
    """
    monkeypatch.delenv("MAX_TOKENS_PER_TICKET", raising=False)
    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()
    _patch_mcp(monkeypatch)

    plan = json.dumps(
        [{"tool": "access_grant_access", "args": {"username": "tuser", "resource": "vpn"}, "reasoning": "r"}]
    )
    llm = _MeteredLLM(replies=["ACCESS_CHANGE", "tuser", plan], tokens_per_call=60)
    monkeypatch.setattr(graph_module, "FallbackLLM", lambda: llm)

    graph = compile_graph(checkpointer=InMemorySaver())
    token_budget.start_accounting(0)
    result = await graph.ainvoke(
        {
            "messages": [], "ticket_id": 999_002, "ticket_text": "Grant vpn to tuser",
            "category": "", "plan": [], "plan_index": 0, "pending_approval_id": None,
            "results": [], "done": False, "error": None, "replan_count": 0, "tokens_used": 0,
        },
        config={"configurable": {"thread_id": "budget-test-2"}},
    )
    get_settings.cache_clear()

    assert result["error"] is None
    assert result["done"] is True
    assert [r["ok"] for r in result["results"]] == [True]
    # Spend was still ACCOUNTED (state records it) even with no limit set.
    assert result["tokens_used"] > 0


async def test_budget_metric_increments_on_abort(monkeypatch):
    monkeypatch.setenv("MAX_TOKENS_PER_TICKET", "10")
    monkeypatch.setenv("SENSITIVE_ACTIONS", "disable_user,revoke_access")
    get_settings.cache_clear()
    _patch_mcp(monkeypatch)

    llm = _MeteredLLM(replies=["ACCESS_CHANGE"], tokens_per_call=60)
    monkeypatch.setattr(graph_module, "FallbackLLM", lambda: llm)

    before = _sample("ticket_token_budget_exceeded_total")
    graph = compile_graph(checkpointer=InMemorySaver())
    token_budget.start_accounting(0)
    await graph.ainvoke(
        {
            "messages": [], "ticket_id": 999_003, "ticket_text": "Grant vpn to tuser",
            "category": "", "plan": [], "plan_index": 0, "pending_approval_id": None,
            "results": [], "done": False, "error": None, "replan_count": 0, "tokens_used": 0,
        },
        config={"configurable": {"thread_id": "budget-test-3"}},
    )
    get_settings.cache_clear()

    assert _sample("ticket_token_budget_exceeded_total") == before + 1


def test_add_tokens_outside_a_run_is_noop():
    """LLM helpers called outside any ticket run (no accumulator) must not
    crash or leak accounting across tests."""
    token_budget._run_tokens.set(None)
    token_budget.add_tokens(50)
    assert token_budget.current_total() is None
    assert token_budget.budget_exceeded() is False
