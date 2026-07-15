"""Golden-ticket eval runner: drives the REAL compiled graph per ticket —
classifier, username extraction + PII-masked observation, planner JSON
guardrails, sensitivity gating, routing — with only two things substituted:

- the MCP boundary: a canned employee directory stands in for the identity
  server (so no subprocess/DB fixtures), installed at the same three seams
  the unit tests use (mcp_session / call_tool / discover_tool_reference);
- the LLM: either a scripted replayer (CI) or the real configured model
  (live) — the caller decides via llm_factory.

Scoring per ticket (see evals/golden.py for the contract): category, plan
tool sequence, per-step arg subsets, forbidden tools, and whether the HITL
gate interrupted the run. Ticketing_* steps are stripped from the actual
plan before comparison — models legitimately differ on whether to add a
courtesy ticket comment, and pinning that would make live evals flaky
without protecting anything (comments are non-sensitive and read-only in
effect).
"""

import json
import logging
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any, Callable
from unittest.mock import patch

from langgraph.checkpoint.memory import InMemorySaver

from app.agent import graph as graph_module
from app.agent import token_budget
from app.agent.graph import compile_graph
from evals.golden import GOLDEN_TICKETS, GoldenTicket

logger = logging.getLogger(__name__)

_EMPLOYEES: dict[str, dict[str, Any]] = {
    "jsmith": {
        "username": "jsmith", "full_name": "John Smith", "email": "john.smith@corp.example.com",
        "department": "Engineering", "status": "active", "manager_username": "mchen",
        "access_grants": ["vpn", "github:engineering"],
    },
    "rlee": {
        "username": "rlee", "full_name": "Rachel Lee", "email": "rachel.lee@corp.example.com",
        "department": "Sales", "status": "active", "manager_username": "mchen",
        "access_grants": ["crm"],
    },
    "adavis": {
        "username": "adavis", "full_name": "Alice Davis", "email": "alice.davis@corp.example.com",
        "department": "Marketing", "status": "disabled", "manager_username": "mchen",
        "access_grants": [],
    },
    "ceo": {
        "username": "ceo", "full_name": "Casey Executive", "email": "ceo@corp.example.com",
        "department": "Executive", "status": "active", "manager_username": "ceo",
        "access_grants": ["admin-panel"],
    },
}

_TOOL_REFERENCE = """- identity_get_user(username) -> look up an employee record
- identity_create_user(username, full_name, email, department) -> create an employee account (SENSITIVE: requires approval)
- identity_disable_user(username) -> disable an employee account (SENSITIVE: requires approval)
- identity_enable_user(username) -> re-activate a previously disabled account (SENSITIVE: requires approval)
- access_grant_access(username, resource) -> grant access to a resource (SENSITIVE: requires approval)
- access_revoke_access(username, resource) -> revoke access to a resource (SENSITIVE: requires approval)
- ticketing_add_ticket_comment(comment) -> add a comment to this ticket
- ticketing_get_ticket_status() -> current ticket status"""


class _FakeSession:
    pass


async def _fake_call_tool(session, tool: str, args: dict) -> str:
    if tool == "identity_get_user":
        username = args.get("username", "")
        record = _EMPLOYEES.get(username)
        if record is None:
            raise RuntimeError(f"MCP tool 'identity_get_user' failed: No such user: {username!r}")
        return json.dumps(record)
    # Mutating tools never actually execute in an eval run — sensitive steps
    # interrupt at the approval gate first. Non-sensitive fallthrough
    # (ticketing comments) just succeeds.
    return json.dumps({"ok": True, "tool": tool})


@asynccontextmanager
async def _fake_mcp_session():
    yield _FakeSession()


async def _fake_discover_tool_reference() -> str:
    return _TOOL_REFERENCE


class ScriptedLLM:
    """Replays recorded replies in call order (classify, username, plan)."""

    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls = 0

    async def ainvoke(self, messages):
        class _Resp:
            content = self.replies[min(self.calls, len(self.replies) - 1)]
            usage_metadata = None

        self.calls += 1
        return _Resp()


@dataclass
class TicketResult:
    name: str
    passed: bool
    failures: list[str] = field(default_factory=list)
    category: str = ""
    plan_tools: list[str] = field(default_factory=list)
    gated: bool = False


@dataclass
class EvalReport:
    results: list[TicketResult]

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def score(self) -> float:
        return self.passed / self.total if self.total else 1.0

    def summary_lines(self) -> list[str]:
        lines = []
        for r in self.results:
            mark = "PASS" if r.passed else "FAIL"
            lines.append(f"[{mark}] {r.name} (category={r.category}, plan={r.plan_tools}, gated={r.gated})")
            for failure in r.failures:
                lines.append(f"       - {failure}")
        lines.append(f"score: {self.passed}/{self.total}")
        return lines


def _score(ticket: GoldenTicket, category: str, plan: list[dict], gated: bool) -> TicketResult:
    failures: list[str] = []
    plan_tools_all = [step.get("tool", "") for step in plan]
    # Courtesy ticketing steps are outside the contract (see module docstring).
    scored_steps = [s for s in plan if not str(s.get("tool", "")).startswith("ticketing_")]
    plan_tools = [s.get("tool", "") for s in scored_steps]

    if category != ticket["expected_category"]:
        failures.append(f"category: expected {ticket['expected_category']!r}, got {category!r}")

    if plan_tools != ticket["expected_tools"]:
        failures.append(f"tools: expected {ticket['expected_tools']}, got {plan_tools}")
    else:
        for i, expected_args in enumerate(ticket["expected_args"]):
            actual_args = scored_steps[i].get("args", {})
            for key, value in expected_args.items():
                if actual_args.get(key) != value:
                    failures.append(
                        f"step {i} ({plan_tools[i]}): arg {key!r} expected {value!r}, "
                        f"got {actual_args.get(key)!r}"
                    )

    for forbidden in ticket["forbidden_tools"]:
        if forbidden in plan_tools_all:
            failures.append(f"forbidden tool {forbidden!r} appeared in the plan")

    if gated != ticket["expects_gate"]:
        failures.append(f"gate: expected gated={ticket['expects_gate']}, got {gated}")

    return TicketResult(
        name=ticket["name"], passed=not failures, failures=failures,
        category=category, plan_tools=plan_tools_all, gated=gated,
    )


async def evaluate(llm_factory: Callable[[GoldenTicket], Any]) -> EvalReport:
    """Runs every golden ticket through the real graph. llm_factory receives
    the ticket and returns the LLM to use (a ScriptedLLM of its recorded
    replies for CI; the real get_llm() result, ignoring the ticket, live).

    Patches graph_module.FallbackLLM (not get_llm) — every agent node
    constructs a FallbackLLM() rather than calling get_llm() directly (see
    app/agent/llm.py's module docstring), so that's the seam to intercept
    to get a fixed llm_factory result injected in place of real provider
    fallback logic.
    """
    results: list[TicketResult] = []
    for index, ticket in enumerate(GOLDEN_TICKETS):
        llm = llm_factory(ticket)
        with (
            patch.object(graph_module, "mcp_session", _fake_mcp_session),
            patch.object(graph_module, "call_tool", _fake_call_tool),
            patch.object(graph_module, "discover_tool_reference", _fake_discover_tool_reference),
            patch.object(graph_module, "FallbackLLM", lambda: llm),
        ):
            graph = compile_graph(checkpointer=InMemorySaver())
            config = {"configurable": {"thread_id": f"eval-{index}-{ticket['name']}"}}
            token_budget.start_accounting(0)
            state = await graph.ainvoke(
                {
                    "messages": [],
                    "ticket_id": 900_000 + index,
                    "ticket_text": f"Subject: {ticket['subject']}\n\n{ticket['body']}",
                    "category": "", "plan": [], "plan_index": 0,
                    "pending_approval_id": None, "results": [], "done": False,
                    "error": None, "replan_count": 0, "tokens_used": 0,
                },
                config=config,
            )
            snapshot = await graph.aget_state(config)
            gated = bool(getattr(snapshot, "interrupts", None) or ())

        results.append(
            _score(ticket, state.get("category", ""), state.get("plan", []), gated)
        )
    return EvalReport(results=results)
