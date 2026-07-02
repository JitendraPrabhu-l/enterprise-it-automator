"""Drives the agent graph, persisting interrupted runs so approval can resume
them later from a separate HTTP request (a different process tick entirely) —
including across an application restart, since the checkpointer is backed by
a SQLite file rather than kept in memory.
"""

import asyncio
from pathlib import Path

from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from langgraph.types import Command

from app.agent.graph import compile_graph
from app.config import get_settings

_graph = None
_checkpointer_cm = None
_init_lock = asyncio.Lock()


async def _get_graph():
    global _graph, _checkpointer_cm
    if _graph is not None:
        return _graph
    async with _init_lock:
        if _graph is None:
            checkpoint_db_path = get_settings().checkpoint_db_path
            Path(checkpoint_db_path).parent.mkdir(parents=True, exist_ok=True)
            _checkpointer_cm = AsyncSqliteSaver.from_conn_string(checkpoint_db_path)
            checkpointer = await _checkpointer_cm.__aenter__()
            _graph = compile_graph(checkpointer=checkpointer)
        return _graph


def _thread_config(ticket_id: int) -> dict:
    return {"configurable": {"thread_id": f"ticket-{ticket_id}"}}


async def start_ticket_run(ticket_id: int, ticket_text: str) -> dict:
    """Runs the graph until completion or the first HITL interrupt."""
    graph = await _get_graph()
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
    result = await graph.ainvoke(initial_state, config=config)
    return await _summarize(graph, ticket_id, result, config)


async def resume_ticket_run(ticket_id: int) -> dict:
    """Resumes a graph previously paused at an await_approval interrupt.

    Safe to call even if the approval was rejected — execute_step / finalize
    will surface the rejection via require_approval raising inside the MCP
    tool call, which the executor already catches and records as a failure.
    """
    graph = await _get_graph()
    config = _thread_config(ticket_id)
    result = await graph.ainvoke(Command(resume=True), config=config)
    return await _summarize(graph, ticket_id, result, config)


async def _summarize(graph, ticket_id: int, result: dict, config: dict) -> dict:
    snapshot = await graph.aget_state(config)
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
