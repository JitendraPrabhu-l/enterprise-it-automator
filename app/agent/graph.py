"""LangGraph agent: ticket -> ReAct-style plan -> MCP tool execution -> HITL gate.

Graph shape (a small DAG, not a linear chain):

    plan -> route -> execute_step -> route -> ... -> finalize
                 \\-> await_approval (interrupt) -/

`route` inspects the next planned action: sensitive actions with no approved
gate yet fall through to `await_approval`, which uses LangGraph's `interrupt()`
to pause the graph entirely until a human resolves the Approval row via the
FastAPI layer. Non-sensitive actions (or already-approved ones) go straight to
`execute_step`, which calls the MCP server as a real MCP client over stdio.
"""

import json
import logging

from langchain_core.messages import AIMessage, HumanMessage, SystemMessage
from langgraph.graph import END, StateGraph
from langgraph.types import interrupt

from app.agent.llm import get_llm
from app.agent.mcp_client import call_tool, mcp_session
from app.agent.state import AgentState
from app.config import get_settings
from app.db.session import session_scope
from app.mcp_server.approval_gate import find_approved
from app.mcp_server.tools import is_sensitive

logger = logging.getLogger(__name__)

USERNAME_EXTRACTION_PROMPT = """Extract the single employee username this IT ticket is \
about. Prefer an explicit username mentioned in the ticket (e.g. "username jsmith" or \
"account jsmith"); otherwise infer one from the employee's name (lowercase, first \
initial + last name, no spaces). Respond with ONLY the username, no prose, no punctuation. \
If no employee is identifiable, respond with exactly: NONE
"""

PLANNER_SYSTEM_PROMPT = """You are an enterprise IT automation agent. You process \
employee onboarding, offboarding, and access-change tickets by planning a sequence \
of tool calls.

Available tools:
- get_user(username) -> look up an employee record
- create_user(username, full_name, email, department) -> onboard a new employee
- grant_access(username, resource) -> grant access to a resource (e.g. "github:engineering", "vpn", "jira:core-platform")
- disable_user(username) -> deactivate an employee's account (SENSITIVE)
- revoke_access(username, resource) -> remove access to a resource (SENSITIVE)

You will be told, as an OBSERVATION, whether the ticket's target employee already \
exists in the system (and their current access, if so) before you plan. Use it:
- If the ticket asks to onboard/enable/create an employee who the observation says \
ALREADY EXISTS and is active, do NOT call create_user again — just grant_access for \
whatever the ticket additionally requests (or return [] if nothing more is needed).
- If the ticket asks to onboard/enable/create an employee who the observation says \
DOES NOT EXIST, call create_user (and get_user is unnecessary — you already know it \
doesn't exist).
- If the ticket asks to offboard/disable/revoke access for an employee who DOES NOT \
EXIST, return [] — there is nothing to act on.
- Never plan a get_user call as a standalone step; the existence check has already \
been done for you via the observation.

Read the ticket and respond with ONLY a JSON array of steps, no prose, no markdown \
fences. Each step is an object: {"tool": "<tool_name>", "args": {...}, "reasoning": "<why>"}.
Use the exact argument names shown above, and the exact username given in the observation. \
If the ticket lacks enough information to act, return an empty array [].
"""


def _unwrap_exception(exc: BaseException) -> BaseException:
    """anyio TaskGroups (used by the MCP stdio transport) wrap failures in an
    ExceptionGroup; under some event-loop configurations this can nest more
    than one level deep, hiding the real tool error behind a generic
    "unhandled errors in a TaskGroup" message. Walk down to the first leaf.
    """
    seen = exc
    while isinstance(seen, BaseExceptionGroup) and seen.exceptions:
        seen = seen.exceptions[0]
    return seen


def _extract_json_array(text: str) -> list[dict]:
    """Best-effort structural parsing guardrail against markdown fences / stray prose."""
    text = text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.startswith("json"):
            text = text[4:]
    start = text.find("[")
    end = text.rfind("]")
    if start == -1 or end == -1 or end < start:
        raise ValueError(f"Planner did not return a JSON array: {text[:200]!r}")
    return json.loads(text[start : end + 1])


async def _extract_username(llm, ticket_text: str) -> str | None:
    response = await llm.ainvoke(
        [
            SystemMessage(content=USERNAME_EXTRACTION_PROMPT),
            HumanMessage(content=f"Ticket:\n{ticket_text}"),
        ]
    )
    raw = response.content if isinstance(response.content, str) else str(response.content)
    stripped = raw.strip()
    if not stripped:
        return None
    username = stripped.strip('"').strip("'").splitlines()[0].strip()
    if not username or username.upper() == "NONE":
        return None
    return username


async def _observe_user(username: str) -> str:
    """Real MCP tool call — the 'Act' + 'Observe' half of ReAct, run once up
    front so the planner reasons from ground truth instead of guessing
    whether the target account already exists.
    """
    try:
        async with mcp_session() as session:
            raw = await call_tool(session, "get_user", {"username": username})
        return f"Employee {username!r} ALREADY EXISTS. Current record: {raw}"
    except Exception as exc:
        leaf = _unwrap_exception(exc)
        return f"Employee {username!r} DOES NOT EXIST ({leaf})."


async def plan_node(state: AgentState) -> dict:
    llm = get_llm()

    username = await _extract_username(llm, state["ticket_text"])
    observation = (
        await _observe_user(username)
        if username
        else "No specific employee username could be identified from the ticket."
    )

    response = await llm.ainvoke(
        [
            SystemMessage(content=PLANNER_SYSTEM_PROMPT),
            HumanMessage(
                content=f"Ticket:\n{state['ticket_text']}\n\nOBSERVATION: {observation}"
            ),
        ]
    )
    raw = response.content if isinstance(response.content, str) else str(response.content)

    try:
        steps = _extract_json_array(raw)
    except (ValueError, json.JSONDecodeError) as exc:
        logger.warning("Planner produced unparseable output: %s", exc)
        return {
            "plan": [],
            "plan_index": 0,
            "error": f"Planner returned malformed JSON: {exc}",
            "done": True,
            "messages": [AIMessage(content=raw)],
        }

    return {
        "plan": steps,
        "plan_index": 0,
        "messages": [AIMessage(content=raw)],
    }


def route_after_plan(state: AgentState) -> str:
    if state.get("error") or not state.get("plan"):
        return "finalize"
    return "route_step"


def route_step(state: AgentState) -> dict:
    return {}


def route_after_step_check(state: AgentState) -> str:
    plan = state["plan"]
    idx = state["plan_index"]
    if idx >= len(plan):
        return "finalize"

    step = plan[idx]
    if is_sensitive(step["tool"]):
        return "await_approval"
    return "execute_step"


async def await_approval_node(state: AgentState) -> dict:
    """Blocks the graph on a human decision via LangGraph's interrupt mechanism.

    On first entry we create the Approval row (if one doesn't already exist for
    this exact tool+args) and raise interrupt() to suspend execution. The API
    layer resolves the Approval out-of-band; resuming the graph re-enters this
    node, finds the now-decided Approval, and either proceeds or aborts the step.
    """
    step = state["plan"][state["plan_index"]]
    ticket_id = state["ticket_id"]

    async with session_scope() as session:
        approval = await find_approved(session, ticket_id, step["tool"], step["args"])

    if approval is not None:
        return {"pending_approval_id": approval.id}

    from app.db.models import Approval, ApprovalStatus, Ticket, TicketStatus

    async with session_scope() as session:
        existing_pending = await session.get(Ticket, ticket_id)
        approval_row = Approval(
            ticket_id=ticket_id,
            tool_name=step["tool"],
            tool_args=step["args"],
            reasoning=step.get("reasoning", ""),
            status=ApprovalStatus.PENDING,
        )
        session.add(approval_row)
        if existing_pending is not None:
            existing_pending.status = TicketStatus.AWAITING_APPROVAL
        await session.flush()
        approval_id = approval_row.id

    interrupt(
        {
            "reason": "sensitive_action_requires_approval",
            "ticket_id": ticket_id,
            "approval_id": approval_id,
            "tool": step["tool"],
            "args": step["args"],
            "agent_reasoning": step.get("reasoning", ""),
        }
    )
    return {"pending_approval_id": approval_id}


def route_after_approval(state: AgentState) -> str:
    return "execute_step"


async def execute_step_node(state: AgentState) -> dict:
    step = state["plan"][state["plan_index"]]
    args = dict(step["args"])
    args["ticket_id"] = state["ticket_id"]

    if is_sensitive(step["tool"]) and state.get("pending_approval_id"):
        args["approval_id"] = state["pending_approval_id"]

    results = list(state.get("results", []))
    try:
        async with mcp_session() as session:
            raw_result = await call_tool(session, step["tool"], args)
        results.append({"tool": step["tool"], "args": step["args"], "result": raw_result, "ok": True})
    except Exception as exc:
        leaf = _unwrap_exception(exc)
        logger.warning("Tool execution failed for %s: %s", step["tool"], leaf)
        results.append({"tool": step["tool"], "args": step["args"], "result": str(leaf), "ok": False})

    return {
        "results": results,
        "plan_index": state["plan_index"] + 1,
        "pending_approval_id": None,
    }


async def finalize_node(state: AgentState) -> dict:
    from app.db.models import Ticket, TicketStatus

    results = state.get("results", [])
    failed = [r for r in results if not r["ok"]]
    error = state.get("error")

    if error:
        status = TicketStatus.FAILED
        summary = error
    elif failed:
        status = TicketStatus.FAILED
        summary = "; ".join(f"{r['tool']} failed: {r['result']}" for r in failed)
    elif not results:
        status = TicketStatus.COMPLETED
        summary = "No actions were required for this ticket."
    else:
        status = TicketStatus.COMPLETED
        summary = "; ".join(f"{r['tool']} -> {r['result']}" for r in results)

    async with session_scope() as session:
        ticket = await session.get(Ticket, state["ticket_id"])
        if ticket is not None:
            ticket.status = status
            ticket.result_summary = summary

    return {"done": True}


def build_graph():
    graph = StateGraph(AgentState)

    graph.add_node("plan", plan_node)
    graph.add_node("route_step", route_step)
    graph.add_node("await_approval", await_approval_node)
    graph.add_node("execute_step", execute_step_node)
    graph.add_node("finalize", finalize_node)

    graph.set_entry_point("plan")
    graph.add_conditional_edges("plan", route_after_plan, {"route_step": "route_step", "finalize": "finalize"})
    graph.add_conditional_edges(
        "route_step",
        route_after_step_check,
        {"await_approval": "await_approval", "execute_step": "execute_step", "finalize": "finalize"},
    )
    graph.add_conditional_edges("await_approval", route_after_approval, {"execute_step": "execute_step"})
    graph.add_conditional_edges(
        "execute_step",
        route_after_step_check,
        {"await_approval": "await_approval", "execute_step": "execute_step", "finalize": "finalize"},
    )
    graph.add_edge("finalize", END)

    return graph


def compile_graph(checkpointer=None):
    return build_graph().compile(checkpointer=checkpointer)
