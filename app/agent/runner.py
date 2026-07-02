"""Drives the agent graph, persisting interrupted runs so approval can resume
them later from a separate HTTP request (a different process tick entirely).
"""

from langgraph.checkpoint.memory import InMemorySaver
from langgraph.types import Command

from app.agent.graph import compile_graph

_checkpointer = InMemorySaver()
_graph = compile_graph(checkpointer=_checkpointer)


def _thread_config(ticket_id: int) -> dict:
    return {"configurable": {"thread_id": f"ticket-{ticket_id}"}}


async def start_ticket_run(ticket_id: int, ticket_text: str) -> dict:
    """Runs the graph until completion or the first HITL interrupt."""
    initial_state = {
        "messages": [],
        "ticket_id": ticket_id,
        "ticket_text": ticket_text,
        "plan": [],
        "plan_index": 0,
        "pending_approval_id": None,
        "results": [],
        "done": False,
        "error": None,
    }
    config = _thread_config(ticket_id)
    result = await _graph.ainvoke(initial_state, config=config)
    return await _summarize(ticket_id, result, config)


async def resume_ticket_run(ticket_id: int) -> dict:
    """Resumes a graph previously paused at an await_approval interrupt.

    Safe to call even if the approval was rejected — execute_step / finalize
    will surface the rejection via require_approval raising inside the MCP
    tool call, which the executor already catches and records as a failure.
    """
    config = _thread_config(ticket_id)
    result = await _graph.ainvoke(Command(resume=True), config=config)
    return await _summarize(ticket_id, result, config)


async def _summarize(ticket_id: int, result: dict, config: dict) -> dict:
    snapshot = await _graph.aget_state(config)
    interrupts = getattr(snapshot, "interrupts", None) or ()
    pending_approval = interrupts[0].value if interrupts else None

    return {
        "ticket_id": ticket_id,
        "done": result.get("done", False),
        "plan": result.get("plan", []),
        "results": result.get("results", []),
        "error": result.get("error"),
        "interrupted": bool(interrupts),
        "pending_approval": pending_approval,
    }
